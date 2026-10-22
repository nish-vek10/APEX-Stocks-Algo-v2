# research/dislocation_study/09_simulate_trailing.py
"""
STEP 9 -- Re-simulate candidate dislocation signals with an initial ATR stop PLUS an optional trailing stop.

Per signal and (initial mult, trail mult) pair:
  initial stop  = entry - max(init_mult * ATR(signal bar), 0.5% of entry)      (also the 1%-risk sizing distance)
  trailing stop = highest high since entry (through the previous bar) - trail_mult * ATR(previous bar); only ratchets UP
  exits (per day, in order): gap through stop (exit at open) -> stop touched (exit at stop) -> Stage 9 (next open) -> time stop
Pool = POOL_DIST_MAX / POOL_S2_MAX / PORT_MIN_PRICE / PORT_START (see study_config.py).
Resumable: one parquet per shard in output/trail_parts/. Re-run until it prints [OK].
Output: output/sim_trades_trail.parquet              Run: python 09_simulate_trailing.py
"""
from __future__ import annotations

import glob
import os
import time
import warnings

import numpy as np
import pandas as pd

import study_config as C

warnings.filterwarnings("ignore")


def sim_one(o, h, l, c, st, atr, n, i, init_mult, trail):
    e = i + 1
    eo = o[e]
    a0 = atr[i]
    sd = a0 * init_mult if (a0 == a0 and a0 > 0) else eo * C.FIXED_STOP_FALLBACK_PCT
    sd = max(sd, eo * C.STOP_FLOOR_PCT)
    sp0 = eo - sd
    sp, hh = sp0, eo
    for j in range(1, n - e):
        k = e + j
        if trail is not None:
            if h[k - 1] > hh:
                hh = h[k - 1]
            a = atr[k - 1]
            if a == a and a > 0:
                cand = hh - trail * a
                if cand > sp:
                    sp = cand
        if o[k] <= sp:
            return eo, sd, sp0, "stop_gap" if sp <= sp0 else "trail_gap", o[k], k, e
        if l[k] <= sp:
            return eo, sd, sp0, "stop_hit" if sp <= sp0 else "trail_stop", sp, k, e
        if st[k] == 9:
            return (eo, sd, sp0, "stage9_exit", o[k + 1], k + 1, e) if k + 1 < n else (eo, sd, sp0, "stage9_exit_eod", c[k], k, e)
        if j >= C.TIME_STOP_DAYS:
            return (eo, sd, sp0, "time_stop", o[k + 1], k + 1, e) if k + 1 < n else (eo, sd, sp0, "time_stop_eod", c[k], k, e)
    return eo, sd, sp0, "end_of_data", c[n - 1], n - 1, e


def main() -> None:
    C.TRAIL_PARTS.mkdir(parents=True, exist_ok=True)
    files = sorted(glob.glob(str(C.SHARDS / "s*.parquet")))
    t0 = time.time()
    for f in files:
        name = os.path.basename(f)
        part = C.TRAIL_PARTS / name
        if part.exists():
            continue
        if time.time() - t0 > float(os.environ.get("STUDY_BUDGET_S", "1e9")):
            print("[PAUSE] time budget reached; re-run to continue")
            return
        D = pd.read_parquet(f)
        rows = []
        for t, g in D.groupby("ticker", sort=False):
            g = g.sort_values("date").reset_index(drop=True)
            m = (g.signal.values & (g.dist200.values <= -C.POOL_DIST_MAX) & (g.days_since_s2.values <= C.POOL_S2_MAX)
                 & (g.date.values >= np.datetime64(C.PORT_START)))
            sig = np.where(m)[0]
            if len(sig) == 0:
                continue
            o, h = g.open.values.astype(float), g.high.values.astype(float)
            l, c = g.low.values.astype(float), g.close.values.astype(float)
            st, dt, atr, n = g.stage.values, g.date.values, g.atr.values.astype(float), len(g)
            for i in sig:
                if i + 1 >= n or not (o[i + 1] >= C.PORT_MIN_PRICE):
                    continue
                for im in C.TRAIL_INIT:
                    for tm in C.TRAIL_MULT:
                        eo, sd, sp, reason, xp, xi, e = sim_one(o, h, l, c, st, atr, n, i, im, tm)
                        rows.append((t, dt[i], dt[e], dt[xi], im, tm if tm else 0.0, eo, sd, sp, xp, reason, xi - e,
                                     (xp - eo) / sd, g.dist200.values[i], g.days_since_s2.values[i],
                                     reason in ("end_of_data", "stage9_exit_eod", "time_stop_eod")))
        pd.DataFrame(rows, columns=["ticker", "signal_date", "entry_date", "exit_date", "init_mult", "trail_mult",
                                    "entry", "stop_dist", "stop_price", "exit_price", "exit_reason", "hold", "pnl_r",
                                    "dist200", "days_since_s2", "open_at_end"]).to_parquet(part)
        print(f"  {name} done  ({time.time() - t0:.0f}s)", flush=True)
    T = pd.concat([pd.read_parquet(p) for p in sorted(glob.glob(str(C.TRAIL_PARTS / "s*.parquet")))], ignore_index=True)
    T.to_parquet(C.SIM_TRAIL)
    print(f"[OK] {len(T):,} rows -> {C.SIM_TRAIL}")


if __name__ == "__main__":
    main()
