# research/dislocation_study/07_simulate_atr_variants.py
"""
STEP 7 -- Re-simulate the candidate dislocation signals under several ATR stop multipliers.

Candidate pool = signals with price >= POOL_DIST_MAX below EMA200, Stage 2 within POOL_S2_MAX bars,
entry >= PORT_MIN_PRICE, signal date >= PORT_START. Same engine replica as step 02 (entry next open,
stop = max(ATR*mult, 0.5% of entry), stop_gap -> stop_hit -> Stage 9 -> time stop), but EVERY signal is
simulated independently (overlap handled later by the portfolio step).
Output: output/sim_trades_atr.parquet          Run: python 07_simulate_atr_variants.py
"""
from __future__ import annotations

import glob
import warnings

import numpy as np
import pandas as pd

import study_config as C

warnings.filterwarnings("ignore")


def sim_one(o, h, l, c, st, n, i, mult, atr_i):
    e = i + 1
    eo = o[e]
    sd = atr_i * mult if (atr_i == atr_i and atr_i > 0) else eo * C.FIXED_STOP_FALLBACK_PCT
    sd = max(sd, eo * C.STOP_FLOOR_PCT)
    sp = eo - sd
    reason, xp, xi = "end_of_data", c[n - 1], n - 1
    for j in range(1, n - e):
        k = e + j
        if o[k] <= sp:
            return eo, sd, sp, "stop_gap", o[k], k, e
        if l[k] <= sp:
            return eo, sd, sp, "stop_hit", sp, k, e
        if st[k] == 9:
            return (eo, sd, sp, "stage9_exit", o[k + 1], k + 1, e) if k + 1 < n else (eo, sd, sp, "stage9_exit_eod", c[k], k, e)
        if j >= C.TIME_STOP_DAYS:
            return (eo, sd, sp, "time_stop", o[k + 1], k + 1, e) if k + 1 < n else (eo, sd, sp, "time_stop_eod", c[k], k, e)
    return eo, sd, sp, reason, xp, xi, e


def main() -> None:
    rows = []
    for f in sorted(glob.glob(str(C.SHARDS / "s*.parquet"))):
        D = pd.read_parquet(f)
        for t, g in D.groupby("ticker", sort=False):
            g = g.sort_values("date").reset_index(drop=True)
            m = (g.signal.values & (g.dist200.values <= -C.POOL_DIST_MAX) & (g.days_since_s2.values <= C.POOL_S2_MAX)
                 & (g.date.values >= np.datetime64(C.PORT_START)))
            sig = np.where(m)[0]
            if len(sig) == 0:
                continue
            o, h = g.open.values.astype(float), g.high.values.astype(float)
            l, c = g.low.values.astype(float), g.close.values.astype(float)
            st, dt, atr, n = g.stage.values, g.date.values, g.atr.values, len(g)
            for i in sig:
                if i + 1 >= n or not (o[i + 1] >= C.PORT_MIN_PRICE):
                    continue
                for mult in C.ATR_GRID:
                    eo, sd, sp, reason, xp, xi, e = sim_one(o, h, l, c, st, n, i, mult, atr[i])
                    rows.append((t, dt[i], dt[e], dt[xi], mult, eo, sd, sp, xp, reason, xi - e, (xp - eo) / sd,
                                 g.dist200.values[i], g.days_since_s2.values[i], int(st[i]),
                                 reason in ("end_of_data", "stage9_exit_eod", "time_stop_eod")))
        print(f"  {f.split('/')[-1].split(chr(92))[-1]} -> {len(rows):,} rows", flush=True)
    T = pd.DataFrame(rows, columns=["ticker", "signal_date", "entry_date", "exit_date", "atr_mult", "entry", "stop_dist",
                                    "stop_price", "exit_price", "exit_reason", "hold", "pnl_r", "dist200",
                                    "days_since_s2", "stage", "open_at_end"])
    T.to_parquet(C.SIM_ATR)
    print(f"[OK] {len(T):,} rows ({len(T) // len(C.ATR_GRID):,} signals x {len(C.ATR_GRID)} ATR multipliers) -> {C.SIM_ATR}")


if __name__ == "__main__":
    main()
