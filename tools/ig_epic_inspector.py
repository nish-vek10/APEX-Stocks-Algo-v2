# tools/ig_epic_inspector.py
"""
IG Epic Inspector
=================
Discovers correct IG epic strings for your account and broker region.
Use this to fill in config/ig_epic_map.yaml before live trading.

Discovery strategy (in order):
1. Search by ticker symbol     -> filter: SHARES + (ticker in epic OR "(24 Hours)" in name)
2. Search by override term(s)  -> same filter (handles renamed tickers e.g. META->FB)
3. Prefix probe                -> try UA/UB/UC/UD/UE/UF/CS with fetch_market_by_epic

Usage:
    python tools/ig_epic_inspector.py --search AAPL
    python tools/ig_epic_inspector.py --validate-all
    python tools/ig_epic_inspector.py --discover-all
    python tools/ig_epic_inspector.py --debug TICKER
    python tools/ig_epic_inspector.py --list-open-positions

Requires IG credentials in .env (IG_IDENTIFIER, IG_PASSWORD, IG_API_KEY, IG_ACCOUNT_ID)
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from core.utils.config_loader import load_production_config, resolve_ig_credentials, load_ig_epic_map
from prod.execution.ig_connector import IGConnector

OUTPUTS_DIR = ROOT / "outputs"

# Known prefix variants for US stock 24h cash CFDs on IG
_US_PREFIXES = ["UA", "UB", "UC", "UD", "UE", "UF", "CS"]

# Terms that disqualify an instrument (ETPs, leveraged, options, funds, warrants)
_EXCLUDE_TERMS = (
    "leverage", "granite", "incomeshares", "yieldmax", "short etp", "long etp",
    "etp securities", "warrant", " put", " call", "daily ", " de)", "(de)",
    "leveraged", "investment trust", "investment plc", " etf", " plc",
    "ultra-short", "betabuilders", "premium income", "equity premium",
    "global growth", "equity core", "bond", "multi-asset",
)

# Search term overrides: when direct ticker search returns no stock (only funds/ETFs)
# Also handles IG using old ticker symbols in epics (e.g. META trades as FB on IG)
_SEARCH_OVERRIDES: dict[str, list[str]] = {
    "META":  ["Meta Platforms"],        # IG epic: UB.D.FB.CASH.IP (old FB ticker)
    "JPM":   ["JPMorgan Chase"],
    "JNJ":   ["Johnson & Johnson", "Johnson Johnson"],
    "V":     ["Visa Inc", "Visa"],
    "UNH":   ["UnitedHealth Group", "UnitedHealth"],
    "XOM":   ["ExxonMobil", "Exxon Mobil", "Exxon"],
    "WMT":   ["Walmart Inc", "Wal-Mart", "Walmart"],
    "PG":    ["Procter & Gamble", "Procter Gamble"],
    "MA":    ["Mastercard Inc", "Mastercard"],
    "HD":    ["Home Depot"],
    "CVX":   ["Chevron Corp", "Chevron"],
    "MRK":   ["Merck & Co", "Merck"],
    "ABBV":  ["AbbVie Inc", "AbbVie"],
    "PEP":   ["PepsiCo Inc", "PepsiCo", "Pepsi"],
}


def _ensure_outputs() -> Path:
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUTS_DIR


def _is_bad_name(name: str) -> bool:
    """Return True if the name contains any disqualifying term."""
    name_lower = name.lower()
    return any(term in name_lower for term in _EXCLUDE_TERMS)


def _is_cash_cfd(epic: str, name: str, itype: str, ticker: str) -> bool:
    """
    Accept a result as a valid undated cash CFD if:
    - Type is SHARES
    - Name is clean (no ETPs/funds/options)
    - EITHER: ticker appears in epic  (normal case: UA.D.AAPL.CASH.IP)
      OR:     "(24 Hours)" in name    (renamed ticker: UB.D.FB.CASH.IP for META)
    """
    if itype != "SHARES":
        return False
    if _is_bad_name(name):
        return False
    ticker_in_epic = f".{ticker}." in epic.upper()
    is_24h = "(24 hours)" in name.lower()
    return ticker_in_epic or is_24h


def _pick_best(candidates: list[dict]) -> dict | None:
    """Prefer exact ticker-in-epic match first, then '(24 Hours)', then first clean."""
    # Priority 1: ticker in epic + 24 hours
    for c in candidates:
        if c.get("_ticker_in_epic") and "(24 hours)" in c["instrument_name"].lower():
            return c
    # Priority 2: 24 hours (handles renamed tickers)
    for c in candidates:
        if "(24 hours)" in c["instrument_name"].lower():
            return c
    # Priority 3: ticker in epic
    for c in candidates:
        if c.get("_ticker_in_epic"):
            return c
    return candidates[0] if candidates else None


def _parse_results(results, ticker: str) -> list[dict]:
    """Extract rows from trading_ig DataFrame or list response."""
    rows = []
    if results is None:
        return rows
    if hasattr(results, "iterrows"):
        for _, row in results.iterrows():
            rows.append({
                "epic": str(row.get("epic", "")),
                "instrument_name": str(row.get("instrumentName", "")),
                "instrument_type": str(row.get("instrumentType", "")),
            })
    elif isinstance(results, list):
        for item in results:
            rows.append({
                "epic": item.get("epic", ""),
                "instrument_name": item.get("instrumentName", ""),
                "instrument_type": item.get("instrumentType", ""),
            })
    return rows


def _search_candidates(service, search_term: str, ticker: str) -> list[dict]:
    """
    Search IG markets, return only clean undated cash CFD candidates.
    Annotates each result with _ticker_in_epic for priority sorting.
    """
    try:
        results = service.search_markets(search_term=search_term)
    except Exception:
        return []
    if results is None or (hasattr(results, "empty") and results.empty):
        return []

    candidates = []
    for row in _parse_results(results, ticker):
        epic = row["epic"]
        name = row["instrument_name"]
        itype = row["instrument_type"]
        if _is_cash_cfd(epic, name, itype, ticker):
            row["_ticker_in_epic"] = f".{ticker}." in epic.upper()
            candidates.append(row)
    return candidates


def _probe_epic(service, ticker: str) -> dict | None:
    """
    Probe known prefix variants directly via fetch_market_by_epic.
    Exact lookup — works when search API misses the instrument entirely.
    """
    for prefix in _US_PREFIXES:
        epic = f"{prefix}.D.{ticker}.CASH.IP"
        time.sleep(0.15)
        try:
            market = service.fetch_market_by_epic(epic)
            instr = market.get("instrument", {})
            snap = market.get("snapshot", {})
            if instr.get("type") == "SHARES":
                return {
                    "epic": epic,
                    "instrument_name": instr.get("name", ""),
                    "instrument_type": "SHARES",
                    "bid": snap.get("bid", ""),
                    "_ticker_in_epic": True,
                }
        except Exception:
            continue
    return None


def discover_all_epics(ig_connector: IGConnector, epic_cfg: dict) -> None:
    """
    For each ticker: search -> override search -> prefix probe.
    Updates ig_epic_map.yaml with all discovered epics.
    """
    service = ig_connector.service
    tickers = list(epic_cfg.get("epics", {}).keys())

    rows = []
    discovered: dict[str, str] = {}

    print(f"\n-- Auto-Discovery ({len(tickers)} tickers) " + "-" * 50)
    print(f"{'TICKER':<8} {'EPIC':<35} {'METHOD':<16} {'STATUS':<10} {'NAME'}")
    print("-" * 110)

    for ticker in tickers:
        best = None
        method = ""

        # Step 1: search by ticker symbol
        time.sleep(0.4)
        candidates = _search_candidates(service, ticker, ticker)
        best = _pick_best(candidates)
        method = "search:ticker"

        # Step 2: search by override terms (company name / renamed ticker)
        if not best and ticker in _SEARCH_OVERRIDES:
            for term in _SEARCH_OVERRIDES[ticker]:
                time.sleep(0.4)
                candidates = _search_candidates(service, term, ticker)
                best = _pick_best(candidates)
                if best:
                    method = f"search:'{term}'"
                    break

        # Step 3: prefix probe (exact lookup, no search)
        if not best:
            best = _probe_epic(service, ticker)
            method = "probe"

        if best:
            discovered[ticker] = best["epic"]
            print(f"{ticker:<8} {best['epic']:<35} {method:<16} {'FOUND':<10} {best['instrument_name']}")
            rows.append({
                "ticker": ticker, "epic": best["epic"], "method": method,
                "status": "FOUND", "instrument_name": best["instrument_name"],
                "instrument_type": best.get("instrument_type", ""), "error": "",
            })
        else:
            print(f"{ticker:<8} {'--':<35} {'--':<16} {'NOT FOUND':<10} no match via search or probe")
            rows.append({
                "ticker": ticker, "epic": "", "method": "--",
                "status": "NOT_FOUND", "instrument_name": "",
                "instrument_type": "", "error": "exhausted all methods",
            })

    print()
    found_n = sum(1 for r in rows if r["status"] == "FOUND")
    print(f"Discovered: {found_n}/{len(tickers)}")
    print()

    out = _ensure_outputs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out / f"ig_epic_discovery_{ts}.csv"
    fieldnames = ["ticker", "epic", "method", "status", "instrument_name", "instrument_type", "error"]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Discovery CSV: {csv_path}")

    if not discovered:
        print("Nothing discovered -- ig_epic_map.yaml NOT updated.")
        return

    _write_epic_map(discovered, epic_cfg)
    print(f"ig_epic_map.yaml updated: {len(discovered)} epics written.")


def _write_epic_map(discovered: dict[str, str], epic_cfg: dict) -> None:
    """Overwrite config/ig_epic_map.yaml, merging discovered + existing entries."""
    cfg_path = ROOT / "config" / "ig_epic_map.yaml"
    existing = epic_cfg.get("epics", {})
    merged = {t: discovered.get(t, existing.get(t, "")) for t in existing}

    lines = [
        "# config/ig_epic_map.yaml",
        "# -- IG Epic Mapping -------------------------------------------------------",
        "# Auto-updated by: python tools/ig_epic_inspector.py --discover-all",
        "#",
        "# Format: {PREFIX}.D.{TICKER}.CASH.IP  (undated 24h cash CFD -- no expiry)",
        "# NOTE: IG may use old ticker symbols in epics (e.g. META trades as FB).",
        "# Only plain SHARES instruments. No options, ETPs, or leveraged products.",
        "# -------------------------------------------------------------------------",
        "",
        "epics:",
    ]
    for ticker, epic in merged.items():
        lines.append(f"  {ticker:<6}: \"{epic}\"")

    lines += [
        "",
        "# Currency code per ticker (USD for all US equities)",
        "currency_codes:",
        "  default: \"USD\"",
        "",
        "# Expiry: \"-\" = undated rolling (correct for CASH.IP epics)",
        "expiry:",
        "  default: \"-\"",
    ]
    cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def search_epic(ig_connector: IGConnector, query: str) -> None:
    """Search IG markets by keyword and print all results."""
    service = ig_connector.service
    if service is None:
        print("ERROR: Not connected.")
        return
    try:
        results = service.search_markets(search_term=query)
        if results is None or (hasattr(results, "empty") and results.empty):
            print(f"No results for: {query}")
            return

        rows_out = []
        print(f"\n-- Search: '{query}' " + "-" * 50)
        print(f"{'EPIC':<35} {'Instrument Name':<40} {'Type':<15}")
        print("-" * 90)
        for row in _parse_results(results, query):
            print(f"{row['epic']:<35} {row['instrument_name']:<40} {row['instrument_type']:<15}")
            rows_out.append({"query": query, **row})
        print()
        if rows_out:
            out = _ensure_outputs()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_path = out / f"ig_search_{query.replace(' ', '_')}_{ts}.csv"
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["query", "epic", "instrument_name", "instrument_type"])
                writer.writeheader()
                writer.writerows(rows_out)
            print(f"Saved: {csv_path}")
    except Exception as exc:
        print(f"Search error: {exc}")


def validate_all_epics(ig_connector: IGConnector, epic_cfg: dict) -> None:
    """Check each epic in ig_epic_map.yaml against the live API. Print + save CSV."""
    service = ig_connector.service
    epics = epic_cfg.get("epics", {})
    rows = []

    print(f"\n-- Epic Validation " + "-" * 60)
    print(f"{'TICKER':<8} {'EPIC':<35} {'STATUS':<12} {'BID':<10} {'NAME'}")
    print("-" * 100)

    for ticker, epic in epics.items():
        if not epic:
            print(f"{ticker:<8} {'(not set)':<35} {'MISSING':<12} {'':10} --")
            rows.append({"ticker": ticker, "epic": "(not set)", "status": "MISSING",
                         "bid": "", "instrument_name": "", "instrument_type": "", "expiry": "", "error": ""})
            continue
        try:
            market = service.fetch_market_by_epic(epic)
            snap = market.get("snapshot", {})
            instr = market.get("instrument", {})
            bid = snap.get("bid", "")
            name = instr.get("name", "")
            print(f"{ticker:<8} {epic:<35} {'OK':<12} {str(bid):<10} {name}")
            rows.append({"ticker": ticker, "epic": epic, "status": "OK",
                         "bid": bid, "instrument_name": name,
                         "instrument_type": instr.get("type", ""),
                         "expiry": instr.get("expiry", ""), "error": ""})
        except Exception as exc:
            print(f"{ticker:<8} {epic:<35} {'ERROR':<12} {'':10} {exc}")
            rows.append({"ticker": ticker, "epic": epic, "status": "ERROR",
                         "bid": "", "instrument_name": "", "instrument_type": "",
                         "expiry": "", "error": str(exc)})

    print()
    ok = sum(1 for r in rows if r["status"] == "OK")
    errors = sum(1 for r in rows if r["status"] == "ERROR")
    missing = sum(1 for r in rows if r["status"] == "MISSING")
    print(f"Summary: {ok} OK  |  {errors} ERROR  |  {missing} MISSING  (total {len(rows)})")
    print()

    out = _ensure_outputs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out / f"ig_epic_validation_{ts}.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "epic", "status", "bid",
                                                "instrument_name", "instrument_type", "expiry", "error"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved: {csv_path}")


def debug_ticker(ig_connector: IGConnector, ticker: str) -> None:
    """Full diagnostic: raw search results + all prefix probe attempts."""
    service = ig_connector.service
    ticker = ticker.upper()

    print(f"\n=== SEARCH RAW: '{ticker}' ===")
    try:
        results = service.search_markets(search_term=ticker)
        if results is None or (hasattr(results, "empty") and results.empty):
            print("  EMPTY / None")
        else:
            rows = _parse_results(results, ticker)
            print(f"  {len(rows)} result(s):")
            for r in rows:
                print(f"    epic={r['epic']:<35} type={r['instrument_type']:<12} name={r['instrument_name']}")
    except Exception as exc:
        print(f"  EXCEPTION: {exc}")

    print(f"\n=== PREFIX PROBE: {ticker} ===")
    for prefix in _US_PREFIXES:
        epic = f"{prefix}.D.{ticker}.CASH.IP"
        time.sleep(0.15)
        try:
            market = service.fetch_market_by_epic(epic)
            instr = market.get("instrument", {})
            snap = market.get("snapshot", {})
            print(f"  {epic:<35} OK  type={instr.get('type','')}  name={instr.get('name','')}  bid={snap.get('bid','')}")
        except Exception as exc:
            print(f"  {epic:<35} ERROR: {exc}")
    print()


def list_open_positions(ig_connector: IGConnector) -> None:
    """Print all currently open positions on the IG account."""
    service = ig_connector.service
    try:
        positions = service.fetch_open_positions()
        if positions is None or (hasattr(positions, "empty") and positions.empty):
            print("No open positions.")
            return

        rows = []
        print(f"\n-- Open Positions " + "-" * 60)
        print(f"{'EPIC':<35} {'SIZE':<8} {'DIRECTION':<10} {'LEVEL':<10} {'DEAL ID'}")
        print("-" * 90)
        if hasattr(positions, "iterrows"):
            for _, row in positions.iterrows():
                epic = str(row.get("epic", ""))
                size = str(row.get("size", ""))
                direction = str(row.get("direction", ""))
                level = str(row.get("level", ""))
                deal_id = str(row.get("dealId", ""))
                print(f"{epic:<35} {size:<8} {direction:<10} {level:<10} {deal_id}")
                rows.append({"epic": epic, "size": size, "direction": direction,
                             "level": level, "deal_id": deal_id})
        print()
        if rows:
            out = _ensure_outputs()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_path = out / f"ig_open_positions_{ts}.csv"
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["epic", "size", "direction", "level", "deal_id"])
                writer.writeheader()
                writer.writerows(rows)
            print(f"Saved: {csv_path}")
    except Exception as exc:
        print(f"Error: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="IG Epic Inspector")
    parser.add_argument("--search", type=str, help="Search epics by keyword (e.g. AAPL)")
    parser.add_argument("--search-many", type=str, nargs="+", metavar="TERM",
                        help="Search multiple tickers in ONE session (e.g. --search-many NFLX AMD INTC CRM)")
    parser.add_argument("--validate-all", action="store_true",
                        help="Validate all epics in config/ig_epic_map.yaml")
    parser.add_argument("--discover-all", action="store_true",
                        help="Auto-discover epics for all tickers (search + probe)")
    parser.add_argument("--debug", type=str, metavar="TICKER",
                        help="Full diagnostic: raw search + prefix probes for one ticker")
    parser.add_argument("--list-open-positions", action="store_true",
                        help="List all open positions on IG account")
    args = parser.parse_args()

    if not any([args.search, args.search_many, args.validate_all, args.discover_all,
                args.debug, args.list_open_positions]):
        parser.print_help()
        sys.exit(0)

    prod_cfg = load_production_config(ROOT)
    ig_creds = resolve_ig_credentials(prod_cfg)
    if not ig_creds.get("api_key"):
        print("ERROR: IG_API_KEY not set. Check .env.")
        sys.exit(1)

    with IGConnector(ig_creds) as conn:
        if args.debug:
            debug_ticker(conn, args.debug)
        if args.search:
            search_epic(conn, args.search)
        if args.search_many:
            for term in args.search_many:
                search_epic(conn, term)
                time.sleep(0.5)
        if args.discover_all:
            epic_cfg = load_ig_epic_map(ROOT)
            discover_all_epics(conn, epic_cfg)
        if args.validate_all:
            epic_cfg = load_ig_epic_map(ROOT)
            validate_all_epics(conn, epic_cfg)
        if args.list_open_positions:
            list_open_positions(conn)


if __name__ == "__main__":
    main()
