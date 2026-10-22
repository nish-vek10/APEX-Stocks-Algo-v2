# research/dislocation_study/02_simulate_trades.py
"""
STEP 2 -- Simulate every signal with an exact replica of ALGO-Stocks
backtest/engine.py::simulate_trade, with the baseline's overlap rule
(overlap_mode: disabled = one position per ticker at a time; this is also how
production behaves: an already-open ticker is skipped).

Exit order per day j>0: stop_gap (open<=stop) -> stop_hit (low<=stop) ->
Stage 9 (exit next open) -> time stop (365 bars). Entry = next bar's open.
Stop distance = max(2*ATR(signal bar), 0.5% of entry open).

Also writes 00_parity_validation.csv: comparison against the original backtest
trades (ALGO-Stocks universe_baseline_v1_20260224_2310) on the overlapping window.

Run:  python 02_simulate_trades.py
"""
from __future__ import annotations

import glob
import warnings

import numpy as np
import pandas as pd

import study_config as C

warnings.filterwarnings("ignore")

COLS = ["ticker", "signal_date", "entry_date", "exit_date", "taken", "entry", "stop_dist",
        "stop_price", "exit_price", "exit_reason", "hold", "pnl_r", "close", "e10", "e20",
        "e50", "e200", "bb_lower", "bb_mid", "don_up", "volratio", "rsi", "stage",
        "prev_stage", "days_since_s2", "atr", "atr_pct", "dist200", "min_dist200_60",
        "dd120", "ret20"]


def simulate() -> pd.DataFrame:
    out = []
    for f in sorted(glob.glob(str(C.SHARDS / "s*.parquet"))):
        D = pd.read_parquet(f)
        for t, g in D.groupby("ticker", sort=False):
            g = g.sort_values("date").reset_index(drop=True)
            sig = np.where(g.signal.values)[0]
            if len(sig) == 0:
                continue
            o, h = g.open.values.astype(float), g.high.values.astype(float)
            l, c = g.low.values.astype(float), g.close.values.astype(float)
            st, dt, atr, n = g.stage.values, g.date.values, g.atr.values, len(g)
            last_exit = np.datetime64("1900-01-01")
            for i in sig:
                if i + 1 >= n:
                    continue
                e = i + 1
                eo = o[e]
                if not (eo > 0):
                    continue
                a = atr[i]
                sd = a * C.ATR_MULT if (a == a and a > 0) else eo * C.FIXED_STOP_FALLBACK_PCT
                sd = max(sd, eo * C.STOP_FLOOR_PCT)
                sp = eo - sd
                reason, xp, xi = "end_of_data", c[n - 1], n - 1
                for j in range(1, n - e):
                    k = e + j
                    if o[k] <= sp:
                        reason, xp, xi = "stop_gap", o[k], k
                        break
                    if l[k] <= sp:
                        reason, xp, xi = "stop_hit", sp, k
                        break
                    if st[k] == 9:
                        if k + 1 < n:
                            reason, xp, xi = "stage9_exit", o[k + 1], k + 1
                        else:
                            reason, xp, xi = "stage9_exit_eod", c[k], k
                        break
                    if j >= C.TIME_STOP_DAYS:
                        if k + 1 < n:
                            reason, xp, xi = "time_stop", o[k + 1], k + 1
                        else:
                            reason, xp, xi = "time_stop_eod", c[k], k
                        break
                taken = bool(dt[e] > last_exit)
                if taken and dt[xi] > last_exit:
                    last_exit = dt[xi]
                r = g.iloc[i]
                out.append((t, dt[i], dt[e], dt[xi], taken, eo, sd, sp, xp, reason, xi - e,
                            (xp - eo) / sd, r.close, r.ema_10, r.ema_20, r.ema_50, r.ema_200,
                            r.bb_lower, r.bb_mid, r.donchian_upper_s1, r.volume_ratio, r.rsi,
                            r.stage, r.prev_stage, r.days_since_s2, a, r.atr_pct, r.dist200,
                            r.min_dist200_60, r.dd120, r.ret20))
        print(f"  processed {f.split('/')[-1].split(chr(92))[-1]}: trades so far {len(out)}", flush=True)
    T = pd.DataFrame(out, columns=COLS)
    return T


def parity(T: pd.DataFrame) -> None:
    if not C.BT_TRADES.exists():
        print("[WARN] backtest trades file not found; skipping parity check")
        return
    b = pd.read_parquet(C.BT_TRADES)
    b = b[b.exit_reason != "end_of_data"]
    a = T[(T.taken) & (T.signal_date >= "2022-01-31") & (T.signal_date <= "2026-01-29")
          & (T.exit_reason != "end_of_data")]
    m = a.merge(b[["ticker", "signal_date", "pnl_r", "exit_reason", "entry_price"]],
                on=["ticker", "signal_date"], suffixes=("", "_bt"), how="outer", indicator=True)
    both = m[m._merge == "both"]
    rep = pd.DataFrame([{
        "sim_trades_taken_in_window": len(a),
        "backtest_trades": len(b),
        "matched": len(both),
        "sim_only": int((m._merge == "left_only").sum()),
        "backtest_only": int((m._merge == "right_only").sum()),
        "pct_pnl_r_within_0.01": round(float((abs(both.pnl_r - both.pnl_r_bt) < 0.01).mean()) * 100, 2),
        "pct_same_exit_reason": round(float((both.exit_reason == both.exit_reason_bt).mean()) * 100, 2),
        "pct_same_entry_price": round(float((abs(both.entry - both.entry_price) < 0.01).mean()) * 100, 2),
    }])
    rep.to_csv(C.OUT / "00_parity_validation.csv", index=False)
    print(rep.T.to_string(header=False))


def main() -> None:
    C.OUT.mkdir(parents=True, exist_ok=True)
    T = simulate()
    T.to_parquet(C.SIM_TRADES)
    print(f"[OK] {len(T)} simulated signals ({int(T.taken.sum())} taken) "
          f"{T.signal_date.min().date()} -> {T.signal_date.max().date()}")
    parity(T)


if __name__ == "__main__":
    main()
