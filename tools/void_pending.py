# tools/void_pending.py
"""
Void pending signals / fired-signal keys / pending Stage-9 exits generated on
a bad (e.g. partial intraday) bar. Backs up state/run_state.json first.

Usage (scheduler OFF):
    python tools/void_pending.py --date 2026-10-06 --clear-stage9
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state" / "run_state.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="signal_date to void, YYYY-MM-DD")
    ap.add_argument("--clear-stage9", action="store_true",
                    help="also clear pending_stage9_exits")
    a = ap.parse_args()

    st = json.loads(STATE.read_text(encoding="utf-8"))
    bak = STATE.with_name(f"run_state.bak_{datetime.now():%Y%m%d_%H%M%S}.json")
    shutil.copy2(STATE, bak)

    pend = st.get("pending_signals", [])
    keep = [p for p in pend if str(p.get("signal_date", ""))[:10] != a.date]
    fired = st.get("fired_signals", [])
    keep_f = [k for k in fired if not k.endswith("|" + a.date)]

    print(f"pending_signals : {len(pend)} -> {len(keep)}")
    print(f"fired_signals   : {len(fired)} -> {len(keep_f)}")
    st["pending_signals"] = keep
    st["fired_signals"] = keep_f
    if a.clear_stage9:
        print(f"stage9_exits    : {st.get('pending_stage9_exits', [])} -> []")
        st["pending_stage9_exits"] = []

    STATE.write_text(json.dumps(st, indent=2), encoding="utf-8")
    print(f"[OK] saved. backup: {bak.name}")


if __name__ == "__main__":
    main()
