# tools/refresh_universe.py
"""
APEX daily universe refresh -- Finviz market-cap eligibility.

Writes state/universe_active.json (eligible = USA, cap >= $300M, non-REIT).
Does NOT touch production.yaml. Consumers: core/utils/universe_state.py.

  - Stocks crossing above $300M join automatically (cache backfills history,
    needs MT5 symbol mapping + 260 bars to trade).
  - Stocks dropping below $300M leave the eligible set: no new signals until
    they re-qualify. Open positions are still managed (see universe_state).
  - Safety guard: if Finviz returns < MIN_EXPECTED tickers, keep the previous
    file and exit non-zero (scheduler retries/alerts).

Usage:
    python tools/refresh_universe.py            # write state file
    python tools/refresh_universe.py --dry-run  # show diff only
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import pandas as pd

from tools.build_scan_universe import (
    MIN_MARKET_CAP_USD, _SKIP, _apply_exclusion_rules,
)
from core.utils.universe_state import active_file, held_tickers

MIN_EXPECTED = 1500   # Finviz glitch guard (normal ~2,800-2,900)


def fetch_table() -> pd.DataFrame:
    url = os.environ.get("FINVIZ_EXPORT_URL", "").strip()
    if not url:
        raise RuntimeError("FINVIZ_EXPORT_URL not set in .env")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    df = pd.read_csv(io.StringIO(raw))
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    if "country" in df.columns:
        df = df[df["country"].str.upper().str.strip() == "USA"]
    df = _apply_exclusion_rules(df)
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    df = fetch_table()
    mcap_col = next((c for c in df.columns if "market" in c and "cap" in c), None)
    tick_col = next((c for c in df.columns if c in ("ticker", "symbol")), None)
    if mcap_col is None or tick_col is None:
        raise RuntimeError(f"Finviz export missing columns: {list(df.columns)}")

    df[mcap_col] = pd.to_numeric(df[mcap_col], errors="coerce")   # $ millions
    df[tick_col] = df[tick_col].astype(str).str.strip().str.upper()
    df = df[df[tick_col].str.replace("-", "", regex=False).str.isalnum()]
    df = df[~df[tick_col].isin(_SKIP)]

    thr = MIN_MARKET_CAP_USD / 1_000_000
    eligible_df = df[df[mcap_col] >= thr]
    eligible = sorted(set(eligible_df[tick_col]))
    below = sorted(set(df[df[mcap_col] < thr][tick_col]))

    if len(eligible) < MIN_EXPECTED:
        print(f"[ERROR] Only {len(eligible)} eligible tickers (< {MIN_EXPECTED}) -- "
              "Finviz glitch? Keeping previous universe_active.json.")
        sys.exit(2)

    prev = []
    f = active_file(ROOT)
    if f.exists():
        try:
            prev = json.loads(f.read_text(encoding="utf-8")).get("eligible", [])
        except Exception:
            pass
    added = sorted(set(eligible) - set(prev)) if prev else []
    dropped = sorted(set(prev) - set(eligible)) if prev else []
    held_dropped = sorted(set(dropped) & held_tickers(ROOT))

    print(f"[UNI] eligible={len(eligible)} below_cap={len(below)} "
          f"added={len(added)} dropped={len(dropped)}")
    if added:
        print(f"[UNI] added   : {added[:40]}{' ...' if len(added) > 40 else ''}")
    if dropped:
        print(f"[UNI] dropped : {dropped[:40]}{' ...' if len(dropped) > 40 else ''}")
    if held_dropped:
        print(f"[UNI] WARNING held positions now below cap (still managed): {held_dropped}")

    if args.dry_run:
        print("[DRY RUN] not written")
        return

    out = {
        "asof_utc": datetime.now(timezone.utc).isoformat(),
        "min_market_cap_usd": MIN_MARKET_CAP_USD,
        "eligible": eligible,
        "n_eligible": len(eligible),
        "added": added,
        "dropped": dropped,
        "mcap_musd": {t: round(float(m), 1) for t, m in
                      zip(eligible_df[tick_col], eligible_df[mcap_col])},
    }
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(out), encoding="utf-8")
    print(f"[OK] wrote {f}")


if __name__ == "__main__":
    main()
