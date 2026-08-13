# tools/export_full_universe.py
"""
APEX — IG Full Universe Exporter

Discovers all undated 24h cash CFD shares on your IG account via
systematic search_markets sweeps. Navigation API unavailable on DEMO.

Strategy:
  Phase 1 — "A Inc" through "Z Inc", "A Corp" through "Z Corp", etc.
             Each query subdivides the large result set so no results
             are cut off by IG's 30-result cap per search.
  Phase 2 — Single company name words as fallback sweeps.

Filters: instrumentType=SHARES + epic ends .CASH.IP + "(24 Hours)" in name.

Usage:
    python tools/export_full_universe.py

Output: outputs/ig_full_universe_YYYYMMDD_HHMMSS.csv
"""
from __future__ import annotations

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

OUTPUTS_DIR = ROOT / "outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

RATE_LIMIT_SLEEP = 2.0   # IG non-trading: 30 req/min → 2s between calls
MAX_RETRIES      = 3
RETRY_SLEEP      = 10.0  # backoff on error

LETTERS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

# Phase 1: "<letter> <suffix>" — catches [letter] + suffix combos
_SUFFIXES = ["Inc", "Corp", "Holdings", "Group", "Technologies", "Ltd",
             "LLC", "Systems", "Networks", "Solutions", "Software",
             "Semiconductor", "Pharma", "Therapeutics", "Biosciences"]

# Phase 2: all 2-letter combos — catches anything missed by suffix search
# Covers names where no common suffix appears (e.g. "Booking Holdings" → "Bo")
_TWO_LETTER_COMBOS = [a + b for a in LETTERS for b in LETTERS]   # AA..ZZ = 676 terms

# Phase 3: industry fallbacks
_FALLBACK_TERMS = [
    "Pharmaceutical", "Biotech", "Financial", "Healthcare", "Energy",
    "Digital", "Capital", "Partners", "Resources", "Communications",
    "Services", "Enterprises", "Brands", "Aerospace", "Defense",
    "Insurance", "Realty", "Properties", "Airlines", "Bancorp",
    "Bancshares", "Trust", "Media", "Retail", "Logistics",
    "Motors", "Electric", "Wireless", "Mobility", "Analytics",
]

def _build_search_terms() -> list[str]:
    terms: list[str] = []
    for suffix in _SUFFIXES:
        for letter in LETTERS:
            terms.append(f"{letter} {suffix}")
    terms.extend(_TWO_LETTER_COMBOS)
    terms.extend(_FALLBACK_TERMS)
    return terms


_SEARCH_TERMS = _build_search_terms()

# Instrument name fragments that disqualify a result
_EXCLUDE_TERMS = (
    "leverage", "yieldmax", "short etp", "long etp", "etp securities",
    "warrant", " put", " call", "daily leveraged", "investment trust",
    " etf", "ultra-short", "premium income", "betabuilders",
)


def _is_bad_name(name: str) -> bool:
    n = name.lower()
    return any(t in n for t in _EXCLUDE_TERMS)


def _extract_ticker(epic: str) -> str:
    parts = epic.split(".")
    return parts[2] if len(parts) >= 3 else epic


def _is_valid(epic: str, name: str, itype: str) -> bool:
    if itype != "SHARES":
        return False
    if not epic.upper().endswith(".CASH.IP"):
        return False
    if "(24 hours)" not in name.lower():
        return False
    if _is_bad_name(name):
        return False
    return True


def _search_with_retry(service, term: str) -> list[dict]:
    """Search with retry on error. Returns list of raw row dicts."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = service.search_markets(search_term=term)
            if df is None or (hasattr(df, "empty") and df.empty):
                return []
            return df.to_dict("records") if hasattr(df, "to_dict") else []
        except Exception as exc:
            err = repr(exc) or type(exc).__name__
            if attempt < MAX_RETRIES:
                print(f"  [{term}] attempt {attempt} error: {err} — retrying in {RETRY_SLEEP}s")
                time.sleep(RETRY_SLEEP)
            else:
                print(f"  [{term}] failed after {MAX_RETRIES} attempts: {err}")
    return []


def main() -> None:
    prod_cfg = load_production_config(ROOT)
    creds    = resolve_ig_credentials(prod_cfg)

    print(f"\nConnecting to IG ({creds.get('acc_type', 'DEMO')})...")
    connector = IGConnector(creds)

    results:    list[dict] = []
    seen_epics: set[str]   = set()
    total_searches  = 0
    total_raw_hits  = 0

    total_terms = len(_SEARCH_TERMS)
    print(f"Connected. Running {total_terms} search sweeps (~{total_terms * RATE_LIMIT_SLEEP / 60:.0f} min)...\n")

    with connector:
        service = connector.service

        for i, term in enumerate(_SEARCH_TERMS, 1):
            time.sleep(RATE_LIMIT_SLEEP)
            rows = _search_with_retry(service, term)
            total_searches += 1
            total_raw_hits += len(rows)
            new_this_term = 0

            for row in rows:
                epic   = str(row.get("epic", ""))
                name   = str(row.get("instrumentName", ""))
                itype  = str(row.get("instrumentType", ""))
                curr   = str(row.get("currency", "N/A"))
                status = str(row.get("marketStatus", "N/A"))

                if epic in seen_epics:
                    continue
                if not _is_valid(epic, name, itype):
                    continue

                seen_epics.add(epic)
                results.append({
                    "ticker_in_epic": _extract_ticker(epic),
                    "epic":           epic,
                    "instrument_name": name,
                    "currency":       curr,
                    "market_status":  status,
                    "search_term":    term,
                })
                new_this_term += 1

            if new_this_term:
                print(f"  [{i:3d}/{total_terms}] {term:<25}  +{new_this_term:3d} new  |  total: {len(results)}")

    print(f"\nSweep complete.")
    print(f"  Searches run    : {total_searches}")
    print(f"  Raw API results : {total_raw_hits}")
    print(f"  Unique CFDs     : {len(results)}")

    results.sort(key=lambda x: x["ticker_in_epic"])

    ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUTS_DIR / f"ig_full_universe_{ts}.csv"
    fields   = ["ticker_in_epic", "epic", "instrument_name", "currency",
                "market_status", "search_term"]

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)

    tradeable = sum(1 for r in results if r["market_status"] == "TRADEABLE")
    print(f"\n{'─'*60}")
    print(f"Total instruments : {len(results)}")
    print(f"Tradeable now     : {tradeable}")
    print(f"Saved             : {out_path}")
    print(f"{'─'*60}\n")


if __name__ == "__main__":
    main()
