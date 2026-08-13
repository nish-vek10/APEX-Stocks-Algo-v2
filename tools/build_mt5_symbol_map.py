# tools/build_mt5_symbol_map.py
"""
APEX -- MT5 Symbol Map Builder (catalogue-based, no MT5 connection needed)

Maps config/production.yaml's universe against the cached full stock-CFD
catalogue (tools/output/mt5_stock_catalogue_latest.csv, built by
tools/mt5_full_stock_catalogue.py) and writes config/mt5_symbol_map.yaml.

This is the step to re-run after every tools/build_scan_universe.py --apply
(Finviz universe refresh) -- it's pure local file matching, no MT5 terminal
connection required, so it's instant. Only re-run mt5_full_stock_catalogue.py
itself (which DOES need a live MT5 connection) when you want to refresh what
IC Markets actually offers, e.g. monthly or if execution starts failing on
previously-mapped symbols.

Usage:
    python tools/build_mt5_symbol_map.py
    python tools/build_mt5_symbol_map.py --apply     # write config/mt5_symbol_map.yaml
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PRODUCTION_YAML = ROOT / "config" / "production.yaml"
SYMBOL_MAP_YAML = ROOT / "config" / "mt5_symbol_map.yaml"
CATALOGUE_CSV = ROOT / "tools" / "output" / "mt5_stock_catalogue_latest.csv"

# Mirrors tools/mt5_full_stock_catalogue.py's base_ticker() regex. Re-derived
# here from mt5_symbol directly rather than trusting the catalogue CSV's
# base_ticker column, which was NaN for ~74% of rows (5,417/7,275) under the
# old regex that didn't handle the "-24" extended-hours suffix. Re-running
# this script picks up the fix instantly from the cached CSV -- no MT5
# reconnect needed.
_SUFFIX_RE = re.compile(r"^([A-Z][A-Z0-9\-]*?)(\.(NAS|NYSE|US|NYS|OTC|N|O|A))?(-24)?$")

# Preference order when a ticker has multiple listings/suffixes in the
# catalogue (e.g. primary US listing vs a secondary/ADR-style entry).
# Standard-hours listings always outrank their "-24" (24/5 extended-hours)
# counterpart when both exist, since standard listings are typically the
# more liquid / tighter-spread instrument.
_SUFFIX_PRIORITY = [".NAS", ".NYSE", ".US", ".NYS", ".N", ".O", ".A", ""]


def _derive_base_ticker(mt5_symbol: str) -> str | None:
    m = _SUFFIX_RE.match(mt5_symbol)
    return m.group(1) if m else None


def _suffix_rank(mt5_symbol: str, base_ticker: str) -> int:
    is_24 = mt5_symbol.endswith("-24")
    suffix = mt5_symbol[len(base_ticker):]
    if is_24:
        suffix = suffix[: -3]  # strip "-24" to look up the base suffix rank
    try:
        rank = _SUFFIX_PRIORITY.index(suffix)
    except ValueError:
        rank = len(_SUFFIX_PRIORITY)
    return rank * 2 + (1 if is_24 else 0)


def load_universe_tickers() -> list[str]:
    cfg = yaml.safe_load(PRODUCTION_YAML.read_text(encoding="utf-8"))
    return sorted({str(t).strip().upper() for t in cfg["universe"]["tickers"]})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not CATALOGUE_CSV.exists():
        print(f"[ERROR] {CATALOGUE_CSV} not found.")
        print("Run tools/mt5_full_stock_catalogue.py first (needs a live MT5 connection).")
        sys.exit(1)

    universe = load_universe_tickers()
    cat = pd.read_csv(CATALOGUE_CSV)
    cat["base_ticker"] = cat["mt5_symbol"].astype(str).apply(_derive_base_ticker)
    cat = cat.dropna(subset=["base_ticker"])
    cat["base_ticker"] = cat["base_ticker"].astype(str).str.upper()

    # Resolve duplicates: keep the best-ranked suffix per base ticker
    cat["_rank"] = cat.apply(lambda r: _suffix_rank(str(r["mt5_symbol"]), r["base_ticker"]), axis=1)
    cat = cat.sort_values("_rank").drop_duplicates("base_ticker", keep="first")

    lookup = dict(zip(cat["base_ticker"], cat["mt5_symbol"]))

    mapped = {t: lookup[t] for t in universe if t in lookup}
    unmapped = [t for t in universe if t not in lookup]

    print(f"\n{'─'*60}")
    print(f"Universe (production.yaml) : {len(universe)}")
    print(f"Catalogue (IC Markets)     : {len(cat)} unique base tickers")
    print(f"Mapped                     : {len(mapped)}")
    print(f"Unmapped (scan-only)       : {len(unmapped)}")
    print(f"{'─'*60}")

    if not args.apply:
        print("\nDRY RUN -- pass --apply to write config/mt5_symbol_map.yaml")
        print("\nSample mapped (first 20):")
        for t in sorted(mapped)[:20]:
            print(f"  {t:<10} -> {mapped[t]}")
        return

    header = f"""# config/mt5_symbol_map.yaml
# ─── MT5 Symbol Mapping ─────────────────────────────────────────────────────────
# Ticker -> IC Markets MT5 broker symbol string.
# Built by tools/build_mt5_symbol_map.py from tools/output/mt5_stock_catalogue_latest.csv
# ({len(cat)} unique base tickers in IC Markets' full stock CFD catalogue).
# {len(mapped)}/{len(universe)} of the current universe mapped.
#
# Tickers with an MT5 symbol mapped here execute; everything else in the universe
# still generates signals (scan-only), same design as config/ig_epic_map.yaml.
#
# To refresh: re-run tools/build_scan_universe.py --apply (new universe), then
# this script again --apply. Only re-run tools/mt5_full_stock_catalogue.py
# (needs live MT5 connection) when IC Markets' own offering may have changed.

symbols:
"""
    lines = [header]
    for t in sorted(mapped):
        lines.append(f'  {t}: "{mapped[t]}"\n')

    lines.append('''
# Filling mode overrides (FOK=1, IOC=2, RETURN=4)
filling_overrides: {}

# Global fallback filling mode
global_filling_mode: "IOC"   # "FOK" | "IOC" | "RETURN"
''')

    SYMBOL_MAP_YAML.write_text("".join(lines), encoding="utf-8")
    print(f"\nWritten: {SYMBOL_MAP_YAML}")


if __name__ == "__main__":
    main()
