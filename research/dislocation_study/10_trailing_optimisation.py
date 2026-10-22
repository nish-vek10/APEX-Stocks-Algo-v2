# research/dislocation_study/10_trailing_optimisation.py
"""
STEP 10 -- Trailing-stop + multi-entry optimisation ($100k, 1% risk, 75 cap, NO costs, R cap 10).

Grid: distance below EMA200 x Stage-2 recency x initial ATR stop x trailing ATR multiple (None = no trail)
      x max trades per ticker x gross cap. Same portfolio engine as step 08 (08_multi_entry_study.simulate).
Selection on data before M_SPLIT, validation on/after it (shown untouched).
Output: output/Trailing_Stop_Optimisation.xlsx            Run: python 10_trailing_optimisation.py
"""
from __future__ import annotations

import importlib
import os
import itertools
import time

import numpy as np
import pandas as pd
from openpyxl.chart import LineChart, Reference

import lib_common as L
import study_config as C

m8 = importlib.import_module("08_multi_entry_study")


def fmt(v):
    return "none" if (v is None or v == 0) else f"{v:g}"


def sub(P, dist, s2):
    return P[(P.dist200 <= -dist) & (P.days_since_s2 <= s2)]


def main() -> None:
    A = pd.read_parquet(C.SIM_TRAIL)
    A["stage"] = 7
    A["trail_mult"] = A.trail_mult.fillna(0.0)
    groups = {k: g for k, g in A.groupby(["init_mult", "trail_mult"])}
    combos = list(itertools.product(C.T_DIST, C.T_S2, C.TRAIL_INIT, C.TRAIL_MULT, C.T_MAX_PER_TICKER, C.T_GROSS))
    print(f"[INFO] pool rows {len(A):,}; {len(combos)} scenarios")
    cache_f = C.OUT / "trail_grid_cache_v2.parquet"
    rows, t0 = ([], time.time())
    if cache_f.exists():
        rows = pd.read_parquet(cache_f).to_dict("records")
    done = {r["combo"] for r in rows}
    budget = float(os.environ.get("STUDY_BUDGET_S", "1e9"))
    print(f"[INFO] cached {len(done)} / {len(combos)}")
    for k, (dist, s2, im, tm, mpt, gcap) in enumerate(combos, 1):
        key = f"d{int(dist * 100)}_s{s2}_init{im:g}_trail{fmt(tm)}_t{mpt}_g{int(gcap)}"
        if key in done:
            continue
        if time.time() - t0 > budget:
            pd.DataFrame(rows).to_parquet(cache_f)
            print(f"[PAUSE] {len(rows)}/{len(combos)} done; re-run to continue")
            return
        S = sub(groups[(im, tm or 0.0)], dist, s2)
        if len(S) < 30:
            continue
        r = m8.simulate(S, mpt, gcap, 10.0)
        if r["res"]["trades"] == 0:
            continue
        rows.append({"combo": key, "dist_below_ema200_pct": dist * 100, "stage2_within_bars": s2, "init_atr": im,
                     "trail_atr": fmt(tm), "max_per_ticker": mpt, "gross_cap_x": gcap, **r["res"]})
        if k % 100 == 0:
            print(f"  {k}/{len(combos)} {time.time() - t0:.0f}s", flush=True)
    pd.DataFrame(rows).to_parquet(cache_f)
    G = pd.DataFrame(rows)
    G["trail_sort"] = G.trail_atr.replace("none", "0").astype(float)
    best_sel = G.sort_values("selection_ret_pct", ascending=False).head(25)
    best_cagr = G.sort_values("CAGR_pct", ascending=False).head(25)
    best_calmar = G[G.trades >= 150].sort_values("calmar", ascending=False).head(25)
    # validation-robust: positive in selection AND validation, rank by min(CAGR-ish) -> use geometric mean of both periods
    G["robust_score"] = np.sqrt(np.clip(1 + G.selection_ret_pct / 100, 0.01, None) * np.clip(1 + G.validation_ret_pct / 100, 0.01, None))
    best_robust = G[G.trades >= 150].sort_values("robust_score", ascending=False).head(25)

    marg = []
    for col in ["dist_below_ema200_pct", "stage2_within_bars", "init_atr", "trail_atr", "max_per_ticker"]:
        t = G.groupby(col).agg(scenarios=("combo", "size"), mean_CAGR=("CAGR_pct", "mean"), median_CAGR=("CAGR_pct", "median"),
                               best_CAGR=("CAGR_pct", "max"), mean_maxDD=("max_DD_pct", "mean"),
                               mean_selection_ret=("selection_ret_pct", "mean"),
                               mean_validation_ret=("validation_ret_pct", "mean"), mean_trades=("trades", "mean")).reset_index()
        t.insert(0, "parameter", col)
        marg.append(t.rename(columns={col: "value"}))
    MARG = pd.concat(marg, ignore_index=True)

    top = best_sel.iloc[0]
    hm = G[(G.dist_below_ema200_pct == top.dist_below_ema200_pct) & (G.stage2_within_bars == top.stage2_within_bars)
           & (G.max_per_ticker == top.max_per_ticker)]
    HM = {}
    for metric in ("CAGR_pct", "max_DD_pct", "selection_ret_pct", "validation_ret_pct"):
        HM[metric] = hm.pivot_table(index="init_atr", columns="trail_sort", values=metric).round(1)
        HM[metric].columns = ["no trail" if c == 0 else f"trail {c:g}x" for c in HM[metric].columns]

    cands = list(dict.fromkeys(list(best_sel.combo.head(4)) + list(best_robust.combo.head(3)) + list(best_cagr.combo.head(2))))
    logs, monthly, yearly, curves = {}, {}, {}, {}
    for key in cands:
        row = G[G.combo == key].iloc[0]
        tm = 0.0 if row.trail_atr == "none" else float(row.trail_atr)
        S = sub(groups[(row.init_atr, tm)], row.dist_below_ema200_pct / 100, row.stage2_within_bars)
        r = m8.simulate(S, int(row.max_per_ticker), float(row.gross_cap_x), 10.0)
        d = r["daily"]
        curves[key] = d
        mm = d.resample("ME").last()
        monthly[key] = (mm / mm.shift(1).fillna(C.ACCOUNT_START) - 1) * 100
        ye = d.resample("YE").last()
        yearly[key] = ((ye / ye.shift(1).fillna(C.ACCOUNT_START) - 1) * 100).rename(lambda x: x.year)
        logs[key] = r["trades"]
    MON = pd.DataFrame(monthly)
    MON.index = MON.index.strftime("%Y-%m")
    MON.index.name = "month"
    hl = logs[top.combo]
    MON = MON.join(hl.groupby(hl.entry_date.dt.strftime("%Y-%m")).size().rename("headline_trades_opened")).fillna(
        {"headline_trades_opened": 0})
    YR = pd.DataFrame(yearly).T
    YR.index.name = "combo"
    EQ = pd.DataFrame({k: curves[k].resample("ME").last() for k in cands})
    EQ.index = EQ.index.strftime("%Y-%m")
    EQ.index.name = "month"
    exit_mix = []
    for key in cands[:3]:
        e = logs[key].groupby("exit_reason").agg(trades=("pnl_usd", "size"), total_usd=("pnl_usd", "sum"),
                                                 avg_R=("net_r", "mean"), avg_hold=("hold_days", "mean")).reset_index()
        e.insert(0, "combo", key)
        exit_mix.append(e)

    idx = pd.DataFrame({"sheet": ["Best_By_Selection", "Best_By_Robust", "Best_By_CAGR", "Best_By_Calmar", "Heatmaps",
                                  "Marginal_Effects", "Monthly_Returns", "Yearly_Returns", "Equity_Monthly", "ExitReasons",
                                  "Grid_All"] + [f"Trades_{i + 1}" for i in range(len(cands))] + ["Config"],
                        "content": ["ranked on pre-split return only; validation_ret_pct = untouched later period",
                                    "ranked by geometric mean of selection and validation returns (needs both good)",
                                    "top by full-period CAGR", "top by CAGR / max drawdown",
                                    "init ATR x trail ATR grids for the headline spec", "mean outcome per parameter value",
                                    "portfolio return % per month, candidates side by side", "calendar-year return %",
                                    "month-end equity", "exit mix of top 3 candidates", "every scenario"]
                                   + [f"trade log: {c}" for c in cands] + ["parameters"]})
    cfg = pd.DataFrame({"param": ["account_start", "risk_pct", "max_positions", "max_position_value_pct", "costs",
                                  "min_price", "R cap", "priority", "split", "trail definition", "equity basis"],
                        "value": [C.ACCOUNT_START, C.RISK_PCT, C.MAX_POSITIONS, C.MAX_POS_VALUE_PCT, "NONE (raw spread)",
                                  C.PORT_MIN_PRICE, 10, "deepest below EMA200 first", f"selection < {C.M_SPLIT} <= validation",
                                  "highest high since entry (to prev bar) - trail x ATR(prev bar); ratchets up only",
                                  "realized (open P&L not marked)"]})
    out = C.OUT / "Trailing_Stop_Optimisation.xlsx"
    drop = ["trail_sort"]
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        idx.to_excel(xw, sheet_name="Index", index=False)
        best_sel.drop(columns=drop).to_excel(xw, sheet_name="Best_By_Selection", index=False)
        best_robust.drop(columns=drop).to_excel(xw, sheet_name="Best_By_Robust", index=False)
        best_cagr.drop(columns=drop).to_excel(xw, sheet_name="Best_By_CAGR", index=False)
        best_calmar.drop(columns=drop).to_excel(xw, sheet_name="Best_By_Calmar", index=False)
        row0 = 0
        for name, tbl in HM.items():
            pd.DataFrame({"metric": [f"{name} | {top.combo.split('_init')[0]} | t{int(top.max_per_ticker)}"]}).to_excel(
                xw, sheet_name="Heatmaps", startrow=row0, index=False)
            tbl.reset_index().to_excel(xw, sheet_name="Heatmaps", startrow=row0 + 2, index=False)
            row0 += len(tbl) + 6
        MARG.to_excel(xw, sheet_name="Marginal_Effects", index=False)
        MON.reset_index().to_excel(xw, sheet_name="Monthly_Returns", index=False)
        YR.reset_index().to_excel(xw, sheet_name="Yearly_Returns", index=False)
        EQ.reset_index().to_excel(xw, sheet_name="Equity_Monthly", index=False)
        pd.concat(exit_mix).to_excel(xw, sheet_name="ExitReasons", index=False)
        G.drop(columns=drop).sort_values("CAGR_pct", ascending=False).to_excel(xw, sheet_name="Grid_All", index=False)
        for i, key in enumerate(cands):
            t = logs[key].copy()
            for c in ("signal_date", "entry_date", "exit_date"):
                t[c] = pd.to_datetime(t[c]).dt.date
            t = t.sort_values("entry_date")
            t.insert(0, "trade_no", range(1, len(t) + 1))
            t.to_excel(xw, sheet_name=f"Trades_{i + 1}", index=False)
        cfg.to_excel(xw, sheet_name="Config", index=False)
        ws = xw.sheets["Equity_Monthly"]
        ch = LineChart()
        ch.title = "Month-end realized equity (USD)"
        ch.height, ch.width = 11, 26
        ch.add_data(Reference(ws, min_col=2, max_col=EQ.shape[1] + 1, min_row=1, max_row=len(EQ) + 1), titles_from_data=True)
        ch.set_categories(Reference(ws, min_col=1, min_row=2, max_row=len(EQ) + 1))
        ws.add_chart(ch, "M2")
    L.style_workbook(out)
    print(f"[OK] {out}")
    cols = ["combo", "trades", "win_pct", "CAGR_pct", "max_DD_pct", "selection_ret_pct", "validation_ret_pct", "peak_positions"]
    for nm, t in [("selection", best_sel), ("robust", best_robust), ("CAGR", best_cagr), ("Calmar", best_calmar)]:
        print(f"\nTop 6 by {nm}:")
        print(t[cols].head(6).round(1).to_string(index=False))
    print("\nMarginals:")
    print(MARG[MARG.parameter.isin(["init_atr", "trail_atr", "max_per_ticker"])].round(1).to_string(index=False))


if __name__ == "__main__":
    main()
