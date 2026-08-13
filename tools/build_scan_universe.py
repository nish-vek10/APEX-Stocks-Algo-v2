# tools/build_scan_universe.py
"""
APEX — Scanner Universe Builder

Fetches tickers from Finviz Elite — EXACT replica of ALGO-Stocks backtest
universe construction (research/experiments/04_apply_universe_filters.py):
  - USA-listed equities
  - Market cap >= $300M
  - REIT/real-estate excluded via the same 4-rule engine as
    data/metadata/reit_exclusion.csv (sector_equals "Real Estate";
    industry_contains "REIT" / "REIT -" / "Real Estate")
  - Universe is fully RE-DERIVED each run (not merged with the previous
    production.yaml list) — matches the backtest's fresh-pull philosophy.
    Expect ~2,800-2,900 tickers, not the legacy 528.

Falls back to S&P 500 + NASDAQ 100 from Wikipedia (merged with current) if
FINVIZ_EXPORT_URL not set — degraded path only, not backtest-accurate.

Tickers without an IG epic generate signals but do NOT execute orders.
Orchestrator skips execution if no epic is mapped (by design).

Usage:
    python tools/build_scan_universe.py              # dry run — show diff
    python tools/build_scan_universe.py --apply      # write to production.yaml

Requires .env:
    FINVIZ_EXPORT_URL=https://elite.finviz.com/export.ashx?v=111&auth=YOUR_TOKEN
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import pandas as pd
import yaml

PRODUCTION_YAML = ROOT / "config" / "production.yaml"
IG_EPIC_MAP     = ROOT / "config" / "ig_epic_map.yaml"

# ── Finviz filter thresholds ──────────────────────────────────────────────────
MIN_MARKET_CAP_USD = 300_000_000   # $300M — matches backtest filter exactly

# REIT/real-estate exclusion rules — EXACT replica of ALGO-Stocks
# data/metadata/reit_exclusion.csv (research/experiments/04_apply_universe_filters.py
# apply_exclusions()). Do not add/remove rules here without updating that file too —
# this must stay byte-identical to the backtest's exclusion engine.
_EXCLUSION_RULES = [
    {"rule_type": "sector_equals",      "pattern": "Real Estate"},
    {"rule_type": "industry_contains",  "pattern": "REIT"},
    {"rule_type": "industry_contains",  "pattern": "REIT -"},
    {"rule_type": "industry_contains",  "pattern": "Real Estate"},
]


def _apply_exclusion_rules(df: pd.DataFrame) -> pd.DataFrame:
    """Backtest-identical exclusion engine (sector_equals / industry_contains)."""
    keep = pd.Series(True, index=df.index)
    for r in _EXCLUSION_RULES:
        rule, pattern = r["rule_type"], r["pattern"]
        if rule == "sector_equals" and "sector" in df.columns:
            keep &= df["sector"].astype(str).str.strip() != pattern
        elif rule == "industry_contains" and "industry" in df.columns:
            keep &= ~df["industry"].astype(str).str.contains(pattern, case=False, na=False)
    return df.loc[keep].copy()

# Always skip these regardless of source
_SKIP = {
    "BRK.B", "BRK.A", "BRK-B", "BRK-A",  # Berkshire — yfinance quirky
    "GOOG",                                 # Duplicate of GOOGL
}

_YAML_BOOL_TICKERS = {"ON", "OFF", "YES", "NO", "TRUE", "FALSE"}


def _ticker_yaml(t: str) -> str:
    return f'"{t}"' if t.upper() in _YAML_BOOL_TICKERS else t


# ── Finviz Elite fetch ────────────────────────────────────────────────────────

def fetch_finviz_universe() -> list[str]:
    """
    Download Finviz Elite export and apply backtest-identical filters:
      - Country = USA
      - Market Cap >= $300M
      - Not REIT / real estate
    Returns list of clean tickers.
    """
    url = os.environ.get("FINVIZ_EXPORT_URL", "").strip()
    if not url:
        print("  [WARN] FINVIZ_EXPORT_URL not set in .env — skipping Finviz")
        return []

    print("Fetching Finviz Elite universe...")
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        df = pd.read_csv(io.StringIO(raw))
        print(f"  Raw rows: {len(df)}")

        # Normalise column names
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

        # ── Filter 1: USA only ─────────────────────────────────────────────
        if "country" in df.columns:
            df = df[df["country"].str.upper().str.strip() == "USA"]
            print(f"  After USA filter: {len(df)}")

        # ── Filter 2: Market cap >= $300M ──────────────────────────────────
        mcap_col = next((c for c in df.columns if "market" in c and "cap" in c), None)
        if mcap_col:
            # Finviz exports market cap as millions (e.g. 38293.66 = $38.3B)
            df[mcap_col] = pd.to_numeric(df[mcap_col], errors="coerce")
            df = df[df[mcap_col] >= (MIN_MARKET_CAP_USD / 1_000_000)]
            print(f"  After $300M cap filter: {len(df)}")

        # ── Filter 3: Exclude REITs (exact backtest rule engine) ───────────
        df = _apply_exclusion_rules(df)
        print(f"  After REIT exclusion: {len(df)}")

        # ── Extract tickers ────────────────────────────────────────────────
        ticker_col = next((c for c in df.columns if c in ("ticker", "symbol")), None)
        if ticker_col is None:
            print("  [WARN] No ticker/symbol column found in Finviz export")
            return []

        tickers = [
            str(t).strip().upper()
            for t in df[ticker_col].dropna()
            if str(t).strip()
        ]
        # Remove tickers with special chars that yfinance can't handle
        tickers = [t for t in tickers if t.replace("-", "").isalnum()]

        print(f"  Final Finviz universe: {len(tickers)} tickers")
        return tickers

    except Exception as e:
        print(f"  [WARN] Finviz fetch failed: {e}")
        return []


# ── Wikipedia fallback ────────────────────────────────────────────────────────

_WIKI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def fetch_sp500() -> list[str]:
    print("Fetching S&P 500 from Wikipedia (fallback)...")
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            storage_options={"User-Agent": _WIKI_UA},
        )
        tickers = [t.replace(".", "-") for t in tables[0]["Symbol"].tolist()]
        print(f"  S&P 500: {len(tickers)} tickers")
        return tickers
    except Exception as e:
        print(f"  [WARN] S&P 500 fetch failed: {e}")
        return []


def fetch_nasdaq100() -> list[str]:
    print("Fetching NASDAQ 100 from Wikipedia (fallback)...")
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            storage_options={"User-Agent": _WIKI_UA},
        )
        for df in tables:
            if "Ticker" in df.columns:
                tickers = df["Ticker"].dropna().tolist()
                print(f"  NASDAQ 100: {len(tickers)} tickers")
                return tickers
        return []
    except Exception as e:
        print(f"  [WARN] NASDAQ 100 fetch failed: {e}")
        return []


# ── Config helpers ────────────────────────────────────────────────────────────

def load_current_universe() -> list[str]:
    cfg = yaml.safe_load(PRODUCTION_YAML.read_text(encoding="utf-8"))
    return [str(t) for t in cfg["universe"]["tickers"]]


def load_ig_epic_map() -> dict:
    cfg = yaml.safe_load(IG_EPIC_MAP.read_text(encoding="utf-8"))
    return cfg.get("epics", {})


# ── YAML writer ───────────────────────────────────────────────────────────────

def update_production_yaml(new_tickers: list[str]) -> None:
    text  = PRODUCTION_YAML.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    start_idx = None
    after_idx = None
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped == "tickers:":
            start_idx = i
            continue
        if start_idx is not None and after_idx is None:
            if stripped and not stripped.startswith("- ") and not stripped.startswith("#"):
                after_idx = i
                break

    if start_idx is None:
        print("[ERROR] Could not locate tickers: in production.yaml")
        return
    if after_idx is None:
        after_idx = len(lines)

    epics    = load_ig_epic_map()
    mapped   = sorted(t for t in new_tickers if t in epics)
    unmapped = sorted(t for t in new_tickers if t not in epics)

    block: list[str] = [
        "  tickers:\n",
        "    # ── IG-mapped (signal + execution) ──────────────────────────────\n",
    ]
    for t in mapped:
        block.append(f"    - {_ticker_yaml(t)}\n")
    block.append("    # ── Scan-only (signal only — no IG epic mapped yet) ────────────\n")
    for t in unmapped:
        block.append(f"    - {_ticker_yaml(t)}\n")

    PRODUCTION_YAML.write_text("".join(lines[:start_idx] + block + lines[after_idx:]), encoding="utf-8")

    try:
        cfg     = yaml.safe_load(PRODUCTION_YAML.read_text(encoding="utf-8"))
        written = cfg["universe"]["tickers"]
        print(f"\nproduction.yaml verified: {len(written)} tickers "
              f"({len(mapped)} IG-mapped + {len(unmapped)} scan-only)")
    except Exception as e:
        print(f"\n[WARN] YAML verify failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    current = set(load_current_universe())
    epics   = load_ig_epic_map()

    # Primary: Finviz Elite (matches backtest exactly)
    finviz  = set(fetch_finviz_universe())

    # Fallback: Wikipedia if Finviz not configured
    used_fallback = False
    if not finviz:
        finviz = set(fetch_sp500()) | set(fetch_nasdaq100())
        used_fallback = True

    # Backtest philosophy: universe is fully RE-DERIVED from Finviz each run,
    # not merged with whatever was previously in production.yaml. This matches
    # 04_apply_universe_filters.py in ALGO-Stocks exactly (fresh USA + cap +
    # REIT-exclusion pull every time, no incremental accretion of stale names).
    # The Wikipedia fallback path is the one exception where we merge with
    # current, since it's a degraded/partial source, not the real universe.
    if used_fallback:
        all_tickers = (current | finviz) - _SKIP
    else:
        all_tickers = finviz - _SKIP
    all_tickers = {t for t in all_tickers if t and isinstance(t, str)}
    all_sorted  = sorted(all_tickers)

    added    = all_tickers - current
    removed  = current - all_tickers
    mapped   = {t for t in all_tickers if t in epics}
    unmapped = all_tickers - mapped

    print(f"\n{'─'*60}")
    print(f"Current universe  : {len(current)}")
    print(f"Finviz universe   : {len(finviz)}  (fallback used: {used_fallback})")
    print(f"New universe      : {len(all_tickers)}")
    print(f"Added             : {len(added)}")
    print(f"Removed           : {len(removed)}")
    print(f"IG-mapped (exec)  : {len(mapped)}")
    print(f"Scan-only (signal): {len(unmapped)}")
    print(f"{'─'*60}")

    if not args.apply:
        print("\nDRY RUN — pass --apply to write changes")
        print("\nSample added tickers (first 20):")
        for t in sorted(added)[:20]:
            print(f"  {t:<12} {'epic mapped' if t in epics else 'scan only'}")
        if len(added) > 20:
            print(f"  ... and {len(added) - 20} more")
        print("\nSample removed tickers (first 20):")
        for t in sorted(removed)[:20]:
            print(f"  {t}")
        if len(removed) > 20:
            print(f"  ... and {len(removed) - 20} more")
    else:
        update_production_yaml(all_sorted)
        print("Done.")


if __name__ == "__main__":
    main()
