# tools/export_ig_shares_live.py
"""
APEX — IG Live Account Shares/Stocks Epic Catalogue

Pulls all SHARES-type instruments from a LIVE IG account via exhaustive
2-letter search sweeps. Filters to SHARES only — no indices, FX, commodities.

Credentials loaded from .env using IG_LIVE_* prefix (separate from demo account).
Add to your .env:
    IG_LIVE_API_KEY=944f1c813d946a5fbb985d08d5e7f9c6f788ff5c
    IG_LIVE_IDENTIFIER=your_username
    IG_LIVE_PASSWORD=your_password
    IG_LIVE_ACCOUNT_ID=your_account_id

Output columns:
    ticker          | Extracted from epic (part[2])
    epic            | IG epic string
    instrument_name | Full IG instrument name
    currency        | Instrument currency
    market_status   | TRADEABLE / CLOSED / OFFLINE / etc.
    expiry          | Instrument expiry (DFB = undated)
    search_term     | Which search term discovered this instrument

Strategy:
  Phase 1 — A-Z     (26 single-letter searches)
  Phase 2 — AA-ZZ   (676 two-letter searches — main sweep)
  Phase 3 — Numbers + common stock prefixes (catches numeric tickers)

Usage:
    python tools/export_ig_shares_live.py          # full run
    python tools/export_ig_shares_live.py --test   # quick test (Phase 1 only)

Output: outputs/ig_epics_live/ig_shares_live_YYYYMMDD_HHMMSS_final.csv
        outputs/ig_epics_live/ig_shares_live_YYYYMMDD_HHMMSS_checkpoint.csv
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# ── Output directory ──────────────────────────────────────────────────────────
OUTPUTS_DIR = ROOT / "outputs" / "ig_epics_live"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Timing & reliability ──────────────────────────────────────────────────────
RATE_LIMIT_SLEEP = 2.0    # seconds between API calls (~30 req/min limit)
SEARCH_TIMEOUT   = 15.0   # per-call timeout — abandons stalled requests
MAX_RETRIES      = 3
RETRY_SLEEP      = 10.0
CHECKPOINT_EVERY = 50     # flush to CSV every N searches

# ── Shares-only filter ────────────────────────────────────────────────────────
# Keep only these instrument types — expand list if needed
SHARES_TYPES = {"SHARES", "OPT_SHARES"}
SHARES_ONLY  = True   # set False to pull everything (like the demo script)

# ── Search terms ──────────────────────────────────────────────────────────────
LETTERS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

# Phase 1 — single letters (quick mode / warm-up)
_PHASE1 = list(LETTERS)

# Phase 2 — exhaustive 2-letter combos (AA..ZZ)
_PHASE2 = [a + b for a in LETTERS for b in LETTERS]

# Phase 3 — numeric prefixes + known share prefixes that slip through alpha sweeps
_PHASE3 = [
    # Numbers (catches numeric tickers like 3M, 888, etc.)
    "1", "2", "3", "4", "5", "6", "7", "8", "9",
    "10", "20", "30", "50", "100",
    # Common US/UK equity name starters
    "Inc", "Corp", "Ltd", "PLC", "Holdings", "Group",
    "Tech", "Capital", "Pharma", "Bio", "Health", "Energy",
    "Bank", "Financial", "Insurance", "Real", "Properties",
    # ADRs / foreign listings
    "ADR", "ADS",
]


# ── Credential loader ─────────────────────────────────────────────────────────

def _load_live_creds() -> dict:
    """
    Load LIVE account credentials from .env (IG_LIVE_* prefix).
    Raises ValueError with clear message if any required field is missing.
    """
    required = {
        "api_key":    "IG_LIVE_API_KEY",
        "identifier": "IG_LIVE_IDENTIFIER",
        "password":   "IG_LIVE_PASSWORD",
        "acc_number": "IG_LIVE_ACCOUNT_ID",
    }
    creds: dict = {"acc_type": "LIVE"}
    missing = []

    for field, env_var in required.items():
        val = os.environ.get(env_var, "").strip()
        if not val:
            missing.append(env_var)
        else:
            creds[field] = val

    if missing:
        raise ValueError(
            f"\n[ERROR] Missing LIVE credentials in .env:\n"
            + "\n".join(f"  {v}=<your_value>" for v in missing)
            + "\n\nAdd these to your .env file and retry."
        )

    return creds


# ── Search with timeout + retry ───────────────────────────────────────────────

def _search_with_retry(service, term: str) -> list[dict]:
    """Search with per-call timeout + retry. Returns raw row dicts."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            ex     = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = ex.submit(service.search_markets, search_term=term)
            try:
                df = future.result(timeout=SEARCH_TIMEOUT)
            except concurrent.futures.TimeoutError:
                ex.shutdown(wait=False)
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Export all SHARES epics from IG LIVE account")
    parser.add_argument("--test", action="store_true",
                        help="Quick test: Phase 1 only (26 single-letter searches)")
    args = parser.parse_args()

    # ── Load live credentials ─────────────────────────────────────────────────
    try:
        creds = _load_live_creds()
    except ValueError as e:
        print(e)
        sys.exit(1)

    # ── Build search terms ────────────────────────────────────────────────────
    if args.test:
        search_terms = _PHASE1
        mode_label   = "TEST"
    else:
        search_terms = _PHASE2 + _PHASE3
        mode_label   = "FULL"

    filter_label = "SHARES only" if SHARES_ONLY else "ALL types"
    print(f"\n{mode_label} MODE — {len(search_terms)} searches | filter: {filter_label}")
    print(f"Connecting to IG LIVE (account: {creds.get('acc_number', '?')})...")

    # ── Connect ───────────────────────────────────────────────────────────────
    from prod.execution.ig_connector import IGConnector
    connector = IGConnector(creds)

    # ── File paths ────────────────────────────────────────────────────────────
    ts        = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix    = "_test" if args.test else "_full"
    ckpt_path = OUTPUTS_DIR / f"ig_shares_live{suffix}_{ts}_checkpoint.csv"
    out_path  = OUTPUTS_DIR / f"ig_shares_live{suffix}_{ts}_final.csv"

    fields = ["ticker", "epic", "instrument_name", "currency",
              "market_status", "expiry", "instrument_type", "search_term"]

    # ── State ─────────────────────────────────────────────────────────────────
    results:    list[dict] = []
    seen_epics: set[str]   = set()
    total_raw   = 0
    total_skip  = 0
    t_start     = time.time()
    last_print  = t_start
    ckpt_count  = 0

    def _flush_checkpoint(force: bool = False) -> None:
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
            print(f"  ✓ checkpoint → {ckpt_path.name}  ({ckpt_count} rows)")

    # ── Sweep ─────────────────────────────────────────────────────────────────
    with connector:
        service = connector.service
        print(f"Connected.\n{'─'*65}")
        print(f"  Account type    : {creds['acc_type']}")
        print(f"  Checkpoint file : {ckpt_path.name}")
        print(f"  Flush every     : {CHECKPOINT_EVERY} searches")
        print(f"{'─'*65}")

        for i, term in enumerate(search_terms, 1):
            time.sleep(RATE_LIMIT_SLEEP)
            rows = _search_with_retry(service, term)
            total_raw += len(rows)
            new_this   = 0

            for row in rows:
                itype  = str(row.get("instrumentType", "")).strip()
                epic   = str(row.get("epic", "")).strip()
                name   = str(row.get("instrumentName", "")).strip()
                curr   = str(row.get("currency", "N/A")).strip()
                status = str(row.get("marketStatus", "N/A")).strip()
                expiry = str(row.get("expiry", "DFB")).strip()

                # Shares filter
                if SHARES_ONLY and itype not in SHARES_TYPES:
                    total_skip += 1
                    continue

                if not epic or epic in seen_epics:
                    continue

                parts  = epic.split(".")
                ticker = parts[2] if len(parts) >= 3 else ""

                seen_epics.add(epic)
                results.append({
                    "ticker":          ticker,
                    "epic":            epic,
                    "instrument_name": name,
                    "currency":        curr,
                    "market_status":   status,
                    "expiry":          expiry,
                    "instrument_type": itype,
                    "search_term":     term,
                })
                new_this += 1

            # Checkpoint flush
            if i % CHECKPOINT_EVERY == 0:
                _flush_checkpoint(force=True)
            else:
                _flush_checkpoint(force=False)

            # Progress print
            elapsed  = time.time() - t_start
            eta_sec  = (elapsed / i) * (len(search_terms) - i) if i > 0 else 0
            eta_min  = eta_sec / 60
            now      = time.time()

            if new_this or (now - last_print) >= 30:
                last_print   = now
                elapsed_str  = f"{int(elapsed//60):02d}:{int(elapsed%60):02d}"
                eta_str      = f"{int(eta_min):02d}:{int(eta_sec%60):02d}"
                status_str   = f"+{new_this:4d}" if new_this else "  -- "
                print(
                    f"  [{i:4d}/{len(search_terms)}] {term:<8}  "
                    f"{status_str} new  |  shares: {len(results):>6}  |  "
                    f"elapsed: {elapsed_str}  eta: {eta_str}"
                )

    # Final flush
    _flush_checkpoint(force=True)

    # ── Final output ──────────────────────────────────────────────────────────
    print(f"\nSweep complete.")
    print(f"  Searches run    : {len(search_terms)}")
    print(f"  Raw API results : {total_raw}")
    print(f"  Non-shares skip : {total_skip}")
    print(f"  Unique shares   : {len(results)}")

    # Sort by name
    results.sort(key=lambda x: x["instrument_name"].lower())

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)

    # ── Summary ───────────────────────────────────────────────────────────────
    tradeable = sum(1 for r in results if r["market_status"] == "TRADEABLE")
    by_curr: dict[str, int] = {}
    for r in results:
        c = r["currency"]
        by_curr[c] = by_curr.get(c, 0) + 1

    print(f"\n{'─'*50}")
    print(f"Unique shares epics : {len(results)}")
    print(f"Tradeable now       : {tradeable}")
    print(f"\nBreakdown by currency (top 10):")
    for curr, count in sorted(by_curr.items(), key=lambda x: -x[1])[:10]:
        print(f"  {curr:<10} {count:>5}")
    print(f"{'─'*50}")
    print(f"Final CSV  : {out_path}")
    print(f"Checkpoint : {ckpt_path}")
    print(f"{'─'*50}\n")


if __name__ == "__main__":
    main()
