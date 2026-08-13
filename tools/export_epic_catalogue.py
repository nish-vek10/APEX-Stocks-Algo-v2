# tools/export_epic_catalogue.py
"""
APEX — IG Epic Catalogue Exporter

Connects to IG API, fetches full market details for every epic in
config/ig_epic_map.yaml, and saves a clean CSV to outputs/.

Output columns:
    ticker          | Your strategy ticker (e.g. AAPL)
    epic            | IG epic string (e.g. UA.D.AAPL.CASH.IP)
    instrument_name | Full IG instrument name
    instrument_type | Always SHARES for this universe
    currency        | Instrument currency (USD)
    lot_size        | IG lot size (typically 1 for US shares)
    min_deal_size   | Minimum deal size in lots
    margin_factor   | Margin rate (e.g. 0.20 = 20%)
    bid             | Last bid price
    offer           | Last offer (ask) price
    spread          | offer - bid
    market_status   | TRADEABLE / CLOSED / OFFLINE

Usage:
    python tools/export_epic_catalogue.py

Output saved to: outputs/ig_epic_catalogue_YYYYMMDD_HHMMSS.csv
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

from core.utils.config_loader import (
    load_production_config,
    load_ig_epic_map,
    resolve_ig_credentials,
)
from prod.execution.ig_connector import IGConnector

OUTPUTS_DIR = ROOT / "outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch_market_details(service, epic: str) -> dict:
    """
    Fetch full market details for one epic via IG REST.
    Returns dict with all relevant fields; fills N/A on any error.
    """
    empty = {
        "instrument_name": "N/A",
        "instrument_type": "N/A",
        "currency":        "N/A",
        "lot_size":        "N/A",
        "min_deal_size":   "N/A",
        "margin_factor":   "N/A",
        "bid":             "N/A",
        "offer":           "N/A",
        "spread":          "N/A",
        "market_status":   "N/A",
        "error":           "",
    }
    try:
        market = service.fetch_market_by_epic(epic)
        instr  = market.get("instrument", {})
        snap   = market.get("snapshot", {})
        deal   = market.get("dealingRules", {})

        bid    = snap.get("bid")
        offer  = snap.get("offer")
        spread = round(float(offer) - float(bid), 4) if bid and offer else "N/A"

        # Margin factor: try instrument-level first, then snapshot
        margin_raw = instr.get("marginFactor") or snap.get("marginFactor")
        margin = f"{float(margin_raw):.4f}" if margin_raw else "N/A"

        return {
            "instrument_name": instr.get("name", "N/A"),
            "instrument_type": instr.get("type", "N/A"),
            "currency":        instr.get("currencies", [{}])[0].get("name", "N/A")
                               if instr.get("currencies") else "N/A",
            "lot_size":        instr.get("lotSize", "N/A"),
            "min_deal_size":   deal.get("minDealSize", {}).get("value", "N/A")
                               if isinstance(deal.get("minDealSize"), dict)
                               else deal.get("minDealSize", "N/A"),
            "margin_factor":   margin,
            "bid":             f"{float(bid):.4f}"   if bid   else "N/A",
            "offer":           f"{float(offer):.4f}" if offer else "N/A",
            "spread":          spread,
            "market_status":   snap.get("marketStatus", "N/A"),
            "error":           "",
        }
    except Exception as exc:
        empty["error"] = str(exc)
        return empty


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    prod_cfg  = load_production_config(ROOT)
    epic_cfg  = load_ig_epic_map(ROOT)
    creds     = resolve_ig_credentials(prod_cfg)
    tickers   = prod_cfg["universe"]["tickers"]
    epics_map = epic_cfg.get("epics", {})

    print(f"\nConnecting to IG ({creds.get('acc_type', 'DEMO')})...")
    connector = IGConnector(creds)

    with connector:
        service = connector.service
        print(f"Connected. Fetching {len(tickers)} instruments...\n")

        rows = []
        for ticker in tickers:
            epic = epics_map.get(ticker, "")
            if not epic:
                print(f"  {ticker:<8}  SKIP — no epic in ig_epic_map.yaml")
                rows.append({
                    "ticker": ticker, "epic": "MISSING",
                    "instrument_name": "N/A", "instrument_type": "N/A",
                    "currency": "N/A", "lot_size": "N/A",
                    "min_deal_size": "N/A", "margin_factor": "N/A",
                    "bid": "N/A", "offer": "N/A",
                    "spread": "N/A", "market_status": "N/A",
                    "error": "No epic configured",
                })
                continue

            details = fetch_market_details(service, epic)
            row = {"ticker": ticker, "epic": epic, **details}
            rows.append(row)

            status_marker = "OK" if not details["error"] else f"ERROR: {details['error'][:60]}"
            print(
                f"  {ticker:<8}  {epic:<30}  {details['instrument_name'][:40]:<40}  "
                f"{details['market_status']:<12}  {status_marker}"
            )
            time.sleep(0.15)   # IG rate limit safety

    # ── Write CSV ─────────────────────────────────────────────────────────────

    ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUTS_DIR / f"ig_epic_catalogue_{ts}.csv"

    fieldnames = [
        "ticker", "epic", "instrument_name", "instrument_type",
        "currency", "lot_size", "min_deal_size", "margin_factor",
        "bid", "offer", "spread", "market_status", "error",
    ]

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    ok_count  = sum(1 for r in rows if not r["error"])
    err_count = len(rows) - ok_count

    print(f"\n{'─'*60}")
    print(f"Fetched:  {ok_count}/{len(rows)} OK  |  {err_count} errors")
    print(f"Saved:    {out_path}")
    print(f"{'─'*60}\n")


if __name__ == "__main__":
    main()
