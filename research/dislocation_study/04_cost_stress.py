# research/dislocation_study/04_cost_stress.py
"""
STEP 4 -- Cost / liquidity stress test of the dislocation specs (trade level, in R).

For each spec in study_config.SPECS x slippage x min price x min 20d dollar volume:
n, win%, mean net R (commission + swap + slippage, winsorized), PF per period and the
all-period cluster-bootstrap CI. Answers: does the edge survive realistic fills and
tradable-only names?

Output: output/Cost_Stress_Report.xlsx
Run:    python 04_cost_stress.py
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

import lib_common as L
import study_config as C


def main() -> None:
    T = L.load_all_signals(closed_only=True)
    T = T[T.taken & T.period.isin(L.PERIODS)].reset_index(drop=True)
    print(f"[INFO] closed taken trades 2022+: {len(T):,}  (adv20 coverage {T.adv20.notna().mean():.1%})")

    rows = []
    for (name, spec), slip, pmin, amin in itertools.product(
            C.SPECS.items(), C.SLIPPAGE_GRID, C.MIN_PRICE_GRID, C.MIN_ADV_GRID):
        m = L.spec_mask(T, spec) & (T.entry.values >= pmin)
        if amin > 0:
            m &= (T.adv20.fillna(0).values >= amin)
        S = T[m]
        net = L.net_r(S, slip)
        r = {"spec": name, "slippage_R": slip, "min_price": pmin, "min_adv20_usd": amin}
        for p in L.PERIODS + ["ALL"]:
            pm = np.ones(len(S), bool) if p == "ALL" else (S.period.values == p)
            st = L.stats(net[pm])
            r[f"{p}_n"], r[f"{p}_win_pct"], r[f"{p}_mean_R"], r[f"{p}_pf"] = st["n"], st["win_pct"], st["mean_R"], st["pf"]
        lo, hi = L.cluster_ci(net, S.signal_date.values)
        r["ALL_CI95_low"], r["ALL_CI95_high"] = lo, hi
        r["tickers"] = S.ticker.nunique()
        ok = all((r[f"{p}_n"] >= 20) and (r[f"{p}_mean_R"] > 0) for p in ("TRAIN", "TEST"))
        r["pass_train_test"] = bool(ok)
        r["pass_all_three_CI>0"] = bool(ok and r["OOS26_mean_R"] > 0 and lo > 0)
        rows.append(r)
    R = pd.DataFrame(rows)

    base = R[(R.min_price == 0) & (R.min_adv20_usd == 0)]
    slip_tab = base.pivot_table(index="spec", columns="slippage_R", values="ALL_mean_R")
    breakeven = {}
    for name, spec in C.SPECS.items():
        m = L.spec_mask(T, spec)
        net0 = L.net_r(T[m], 0.0)
        breakeven[name] = float(np.minimum(net0, C.WINSOR_R).mean()) if m.any() else np.nan
    BE = pd.DataFrame({"spec": list(breakeven), "mean_R_at_zero_slippage": list(breakeven.values())})
    BE["breakeven_slippage_R"] = BE.mean_R_at_zero_slippage  # slippage that drives mean R to 0

    idx = pd.DataFrame({"sheet": ["Stress_Full", "Slippage_Matrix", "Breakeven", "Passing", "Config"],
                        "content": ["every combination, all metrics",
                                    "ALL-period mean net R by spec x slippage (no price/ADV filter)",
                                    "slippage in R that kills each spec's edge",
                                    "combinations positive in TRAIN+TEST (and 2026 + CI>0 where flagged)",
                                    "parameters used"]})
    cfg = pd.DataFrame({"param": ["commission_per_share_RT", "swap_R_per_trade", "winsor_R", "bootstrap_reps", "periods"],
                        "value": [C.COMMISSION_PER_SHARE_RT, C.SWAP_R_PER_TRADE, C.WINSOR_R, C.BOOTSTRAP_REPS,
                                  f"TRAIN {C.TRAIN} | TEST {C.TEST} | OOS26 {C.OOS26}"]})
    out = C.OUT / "Cost_Stress_Report.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        idx.to_excel(xw, sheet_name="Index", index=False)
        R.to_excel(xw, sheet_name="Stress_Full", index=False)
        slip_tab.reset_index().to_excel(xw, sheet_name="Slippage_Matrix", index=False)
        BE.to_excel(xw, sheet_name="Breakeven", index=False)
        R[R["pass_all_three_CI>0"]].sort_values("ALL_mean_R", ascending=False).to_excel(xw, sheet_name="Passing", index=False)
        cfg.to_excel(xw, sheet_name="Config", index=False)
    L.style_workbook(out)
    print(f"[OK] {out}")
    show = base[base.slippage_R.isin([0.0, 0.10, 0.20])][
        ["spec", "slippage_R", "TRAIN_mean_R", "TEST_mean_R", "OOS26_mean_R", "ALL_n", "ALL_mean_R", "ALL_CI95_low"]]
    print(show.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
