# research/dislocation_study/11_tier_ladder_study.py
"""
STEP 11 -- Tier ladder: how many setups per day, and what edge, in each zone? (for the APEX Desk tier design)

Events (all entries next open, exit = stop(ATR_MULT x ATR) -> Stage 9 -> 365d, same engine as step 07, no costs):
  BRK   : every Stage 6/7 breakout with Stage-2 history (the production signal), bucketed by distance below EMA200 and Stage-2 age
  W1    : 'near breakout'  - below EMA200 by >=X, Stage 2 within N bars, EMA10>EMA20, close within 3% under the Donchian-20 high
  W2    : 'EMA20 reclaim'  - below EMA200 by >=X, Stage 2 within N bars, close crosses above EMA20 (EMA10 > EMA20 not required)
  W3    : 'EMA50 reclaim'  - same with EMA50
Resumable per shard (output/ladder_parts). Output: output/Tier_Ladder_Study.xlsx
Run: python 11_tier_ladder_study.py   (re-run until [OK])
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
MULT = 3.5
PARTS = C.OUT / "ladder_parts"


def sim(o, h, l, c, st, n, i, atr_i):
    e = i + 1
    eo = o[e]
    sd = atr_i * MULT if (atr_i == atr_i and atr_i > 0) else eo * C.FIXED_STOP_FALLBACK_PCT
    sd = max(sd, eo * C.STOP_FLOOR_PCT)
    sp = eo - sd
    for j in range(1, n - e):
        k = e + j
        if o[k] <= sp:
            return eo, sd, "stop_gap", (o[k] - eo) / sd, k, e
        if l[k] <= sp:
            return eo, sd, "stop_hit", (sp - eo) / sd, k, e
        if st[k] == 9:
            if k + 1 < n:
                return eo, sd, "stage9_exit", (o[k + 1] - eo) / sd, k + 1, e
            return eo, sd, "open_end", (c[k] - eo) / sd, k, e
        if j >= C.TIME_STOP_DAYS:
            return eo, sd, "time_stop", ((o[k + 1] if k + 1 < n else c[k]) - eo) / sd, min(k + 1, n - 1), e
    return eo, sd, "open_end", (c[n - 1] - eo) / sd, n - 1, e


def events(g: pd.DataFrame) -> dict:
    d200 = g.dist200.values
    s2 = g.days_since_s2.values
    c, e10, e20, e50 = g.close.values, g.ema_10.values, g.ema_20.values, g.ema_50.values
    du = g.donchian_upper_s1.values
    base = g.s2_before.values & (d200 <= -0.15) & (s2 <= 90) & (c >= C.PORT_MIN_PRICE)
    stage = g.stage.values
    brk = g.signal.values
    notbrk = ~np.isin(stage, [6, 7])
    w1 = base & (e10 > e20) & (c >= 0.97 * du) & (c < du) & notbrk
    c_prev = np.r_[np.nan, c[:-1]]
    e20_prev = np.r_[np.nan, e20[:-1]]
    e50_prev = np.r_[np.nan, e50[:-1]]
    w2 = base & (c > e20) & (c_prev <= e20_prev) & notbrk
    w3 = base & (c > e50) & (c_prev <= e50_prev) & notbrk
    first = lambda m: m & ~np.r_[False, m[:-1]]          # noqa: E731  first day of a run
    return {"BRK": brk, "W1": first(w1), "W2": w2, "W3": w3}


def main() -> None:
    PARTS.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    budget = float(os.environ.get("STUDY_BUDGET_S", "1e9"))
    for f in sorted(glob.glob(str(C.SHARDS / "s*.parquet"))):
        part = PARTS / os.path.basename(f)
        if part.exists():
            continue
        if time.time() - t0 > budget:
            print("[PAUSE] re-run to continue")
            return
        D = pd.read_parquet(f)
        rows = []
        for t, g in D.groupby("ticker", sort=False):
            g = g.sort_values("date").reset_index(drop=True)
            ev = events(g)
            o, h = g.open.values.astype(float), g.high.values.astype(float)
            l, c = g.low.values.astype(float), g.close.values.astype(float)
            st, dt, atr, n = g.stage.values, g.date.values, g.atr.values.astype(float), len(g)
            lastx = {}
            for kind, m in ev.items():
                idx = np.where(m & (g.date.values >= np.datetime64(C.PORT_START)))[0]
                for i in idx:
                    if i + 1 >= n or not (o[i + 1] >= C.PORT_MIN_PRICE):
                        continue
                    eo, sd, why, r, xi, e = sim(o, h, l, c, st, n, i, atr[i])
                    rows.append((t, kind, dt[i], dt[e], dt[xi], float(g.dist200.values[i]), float(g.days_since_s2.values[i]),
                                 int(st[i]), why, r, xi - e, why == "open_end"))
        pd.DataFrame(rows, columns=["ticker", "kind", "signal_date", "entry_date", "exit_date", "dist200", "days_s2", "stage",
                                    "exit_reason", "r", "hold", "open_end"]).to_parquet(part)
        print(f"  {os.path.basename(f)} ({time.time() - t0:.0f}s)", flush=True)
    E = pd.concat([pd.read_parquet(p) for p in sorted(glob.glob(str(PARTS / "s*.parquet")))], ignore_index=True)
    E = E[~E.open_end].copy()
    E["rw"] = E.r.clip(upper=10)
    E["period"] = np.select([E.signal_date <= pd.Timestamp(C.TRAIN[1]), E.signal_date <= pd.Timestamp(C.TEST[1])],
                            ["TRAIN", "TEST"], default="OOS26")
    days = len(pd.bdate_range(C.PORT_START, E.signal_date.max()))

    def summ(df):
        pos, neg = df.rw[df.rw > 0].sum(), -df.rw[df.rw < 0].sum()
        out = {"n": len(df), "per_day": len(df) / days, "win_pct": (df.r > 0).mean() * 100, "mean_R": df.rw.mean(),
               "median_R": df.r.median(), "pf": pos / neg if neg > 0 else np.nan, "avg_hold": df.hold.mean()}
        for p in ("TRAIN", "TEST", "OOS26"):
            s = df[df.period == p]
            out[f"{p}_n"], out[f"{p}_meanR"] = len(s), s.rw.mean() if len(s) else np.nan
        return pd.Series(out)

    bands = [(0.40, 9), (0.25, 0.40), (0.20, 0.25), (0.15, 0.20), (0.10, 0.15), (0.05, 0.10), (0.0, 0.05), (-9, 0.0)]
    rows = []
    B = E[E.kind == "BRK"]
    for lo, hi in bands:
        m = B[(B.dist200 <= -lo) & (B.dist200 > -hi)] if lo >= 0 else B[B.dist200 > 0]
        for s2lab, s2max in (("S2<=40", 40), ("S2<=60", 60), ("S2<=90", 90), ("S2 any", 1e9)):
            x = m[m.days_s2 <= s2max]
            if len(x) >= 30:
                lab = f"{int(lo * 100)}-{int(hi * 100) if hi < 9 else '+'}% below" if lo >= 0 else "above EMA200"
                rows.append({"kind": "BRK", "zone": lab, "stage2": s2lab, **summ(x).to_dict()})
    LAD = pd.DataFrame(rows)
    W = []
    for kind in ("W1", "W2", "W3"):
        K = E[E.kind == kind]
        for d in (0.15, 0.20, 0.25, 0.30):
            for s2max in (30, 60, 90):
                x = K[(K.dist200 <= -d) & (K.days_s2 <= s2max)]
                if len(x) >= 30:
                    W.append({"kind": kind, "min_dist_below": d * 100, "stage2_within": s2max, **summ(x).to_dict()})
    WARM = pd.DataFrame(W)
    out = C.OUT / "Tier_Ladder_Study.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        LAD.to_excel(xw, sheet_name="Breakout_Ladder", index=False)
        WARM.to_excel(xw, sheet_name="Warming_Setups", index=False)
        pd.DataFrame({"note": [f"ATR stop {MULT}x, no costs, R winsorized at 10, closed trades only, {days} business days",
                               "per_day = average number of setups per day over the whole period",
                               "BRK = production Stage 6/7 breakout with Stage 2 history; W1-W3 = pre-breakout 'warming' setups"]}).to_excel(
            xw, sheet_name="Notes", index=False)
    print(f"[OK] {out}")
    pd.set_option("display.width", 250, "display.max_columns", 30)
    cols = ["zone", "stage2", "n", "per_day", "win_pct", "mean_R", "pf", "TRAIN_meanR", "TEST_meanR", "OOS26_meanR"]
    print(LAD[cols].round(3).to_string(index=False))
    print(WARM[["kind", "min_dist_below", "stage2_within", "n", "per_day", "win_pct", "mean_R", "pf", "TRAIN_meanR", "TEST_meanR", "OOS26_meanR"]].round(3).to_string(index=False))


if __name__ == "__main__":
    main()
