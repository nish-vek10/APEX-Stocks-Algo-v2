# research/dislocation_study/08_multi_entry_study.py
"""
STEP 8 -- Multi-entry + parameter optimisation, $100k account, 1% risk, 75-position cap, NO trading costs.

Grid: distance below EMA200 x Stage-2 recency x ATR stop multiplier x max trades per ticker (1 vs 3)
      x gross-exposure cap x R cap.  Entries: every Stage 6/7 breakout signal in the pool, same-day
      priority = deepest below EMA200 first. Sizing = floor(equity*1%/stop_dist), position value <= 20% equity.
Selection is data-mined, so each combo also shows SELECTION period (before M_SPLIT) vs VALIDATION period
(on/after M_SPLIT); `best_by_selection` ranks on the first, then reports the second untouched.
Output: output/Multi_Entry_Optimisation.xlsx          Run: python 08_multi_entry_study.py
"""
from __future__ import annotations

import heapq
import itertools
import time

import numpy as np
import pandas as pd
from openpyxl.chart import LineChart, Reference

import lib_common as L
import study_config as C

SPLIT = pd.Timestamp(C.M_SPLIT)


def simulate(T: pd.DataFrame, max_per_ticker: int, gross_cap, r_cap) -> dict:
    T = T.copy()
    T["net_r"] = T.pnl_r - C.M_COMMISSION / T.stop_dist - C.M_SWAP_R - C.M_SLIP_R
    if r_cap is not None:
        T["net_r"] = T.net_r.clip(upper=r_cap)
    T = T.sort_values(["entry_date", "dist200"]).reset_index(drop=True)
    eq = C.ACCOUNT_START
    heap, cnt = [], {}
    gross = risk_open = 0.0
    peak_pos, peak_gross, peak_risk = 0, 0.0, 0.0
    sk = {"cap": 0, "ticker": 0, "gross": 0, "size0": 0}
    rows, pts, seq = [], [], 0

    def close_until(d):
        nonlocal eq, gross, risk_open
        while heap and heap[0][0] <= d:
            xd, _, tk, pnl, notional, rsk = heapq.heappop(heap)
            eq += pnl
            gross -= notional
            risk_open -= rsk
            cnt[tk] -= 1
            pts.append((xd, eq))

    for dte, g in T.groupby("entry_date", sort=True):
        close_until(dte)
        for r in g.itertuples(index=False):
            if eq <= 0:
                sk["size0"] += 1
                continue
            if cnt.get(r.ticker, 0) >= max_per_ticker:
                sk["ticker"] += 1
                continue
            if len(heap) >= C.MAX_POSITIONS:
                sk["cap"] += 1
                continue
            sh = min(int(eq * C.RISK_PCT / r.stop_dist), int(eq * C.MAX_POS_VALUE_PCT / r.entry))
            if sh < 1:
                sk["size0"] += 1
                continue
            notional = sh * r.entry
            if gross_cap is not None and gross + notional > gross_cap * eq:
                sk["gross"] += 1
                continue
            rsk = sh * r.stop_dist
            pnl = rsk * r.net_r
            seq += 1
            heapq.heappush(heap, (r.exit_date, seq, r.ticker, pnl, notional, rsk))
            cnt[r.ticker] = cnt.get(r.ticker, 0) + 1
            gross += notional
            risk_open += rsk
            peak_pos = max(peak_pos, len(heap))
            peak_gross = max(peak_gross, gross / eq)
            peak_risk = max(peak_risk, risk_open / eq)
            rows.append((r.ticker, r.signal_date, dte, r.entry, r.stop_price, sh, notional, rsk, r.exit_date,
                         r.exit_price, r.exit_reason, r.hold, pnl, r.net_r, r.open_at_end, eq, r.dist200,
                         r.days_since_s2, cnt[r.ticker]))
    close_until(pd.Timestamp.max)
    tr = pd.DataFrame(rows, columns=["ticker", "signal_date", "entry_date", "entry_price", "stop_price", "shares",
                                     "notional", "risk_usd", "exit_date", "exit_price", "exit_reason", "hold_days",
                                     "pnl_usd", "net_r", "open_at_end", "equity_at_entry", "dist200",
                                     "days_since_s2", "open_trades_same_ticker"])
    if tr.empty:
        return {"res": {"trades": 0}, "daily": pd.Series(dtype=float), "trades": tr}
    ec = pd.DataFrame(pts, columns=["date", "equity"]).groupby("date").equity.last()
    daily = ec.reindex(pd.date_range(pd.Timestamp(C.PORT_START), ec.index.max(), freq="B")).ffill()
    daily = daily.fillna(C.ACCOUNT_START)
    dd = daily / daily.cummax() - 1
    ret = daily.pct_change().dropna()
    yrs = (daily.index[-1] - daily.index[0]).days / 365.25
    final = float(daily.iloc[-1])
    sel, val = daily[daily.index < SPLIT], daily[daily.index >= SPLIT]
    pos, neg = tr.pnl_usd[tr.pnl_usd > 0].sum(), -tr.pnl_usd[tr.pnl_usd < 0].sum()
    res = {
        "trades": len(tr), "win_pct": float((tr.pnl_usd > 0).mean() * 100),
        "final_equity": final, "CAGR_pct": ((final / C.ACCOUNT_START) ** (1 / yrs) - 1) * 100 if final > 0 else -100.0,
        "max_DD_pct": float(dd.min() * 100), "calmar": np.nan,
        "sharpe": float(ret.mean() / ret.std() * np.sqrt(252)) if ret.std() > 0 else np.nan,
        "profit_factor": float(pos / neg) if neg > 0 else np.nan, "avg_net_R": float(tr.net_r.mean()),
        "selection_ret_pct": (sel.iloc[-1] / C.ACCOUNT_START - 1) * 100 if len(sel) else np.nan,
        "validation_ret_pct": (val.iloc[-1] / sel.iloc[-1] - 1) * 100 if len(val) and len(sel) else np.nan,
        "peak_positions": peak_pos, "peak_gross_x": peak_gross, "peak_open_risk_pct": peak_risk * 100,
        "skipped_ticker_cap": sk["ticker"], "skipped_pos_cap": sk["cap"], "skipped_gross": sk["gross"],
        "multi_entry_trades": int((tr.open_trades_same_ticker > 1).sum()),
    }
    res["calmar"] = res["CAGR_pct"] / abs(res["max_DD_pct"]) if res["max_DD_pct"] < 0 else np.nan
    return {"res": res, "daily": daily, "trades": tr}


def main() -> None:
    A = pd.read_parquet(C.SIM_ATR)
    print(f"[INFO] pool rows {len(A):,}; ATR mults {sorted(A.atr_mult.unique())}")
    combos = list(itertools.product(C.M_DIST, C.M_S2, C.ATR_GRID, C.M_MAX_PER_TICKER, C.M_GROSS, C.M_R_CAPS))
    by_mult = {m: g for m, g in A.groupby("atr_mult")}
    out_rows, curves = [], {}
    t0 = time.time()
    for k, (dist, s2, mult, mpt, gcap, rcap) in enumerate(combos, 1):
        P = by_mult[mult]
        S = P[(P.dist200 <= -dist) & (P.days_since_s2 <= s2)]
        if len(S) < 30:
            continue
        r = simulate(S, mpt, gcap, rcap)
        if r["res"]["trades"] == 0:
            continue
        key = f"d{int(dist * 100)}_s{s2}_atr{mult}_t{mpt}_g{int(gcap)}_{'raw' if rcap is None else 'cap' + str(int(rcap))}"
        out_rows.append({"combo": key, "dist_below_ema200_pct": dist * 100, "stage2_within_bars": s2, "atr_mult": mult,
                         "max_per_ticker": mpt, "gross_cap_x": gcap, "r_cap": rcap if rcap else "raw", **r["res"]})
        curves[key] = r["daily"]
        if k % 100 == 0:
            print(f"  {k}/{len(combos)}  {time.time() - t0:.0f}s", flush=True)
    G = pd.DataFrame(out_rows)
    print(f"[INFO] {len(G)} scenarios simulated in {time.time() - t0:.0f}s")

    # ranking views (tail-robust R cap 10, gross 5x = FCA-realistic) ----------------------------------------------
    base = G[(G.r_cap == 10.0) & (G.gross_cap_x == 5.0)].copy()
    best_sel = base.sort_values("selection_ret_pct", ascending=False).head(25)
    best_cagr = G[G.r_cap == 10.0].sort_values("CAGR_pct", ascending=False).head(25)
    best_calmar = base[base.trades >= 150].sort_values("calmar", ascending=False).head(25)
    marg = []
    for col in ["dist_below_ema200_pct", "stage2_within_bars", "atr_mult", "max_per_ticker", "gross_cap_x"]:
        sub = G[G.r_cap == 10.0] if col == "gross_cap_x" else base
        t = sub.groupby(col).agg(scenarios=("combo", "size"), mean_CAGR=("CAGR_pct", "mean"), median_CAGR=("CAGR_pct", "median"),
                                 best_CAGR=("CAGR_pct", "max"), mean_maxDD=("max_DD_pct", "mean"),
                                 mean_trades=("trades", "mean"), mean_validation_ret=("validation_ret_pct", "mean")).reset_index()
        t.insert(0, "parameter", col)
        t = t.rename(columns={col: "value"})
        marg.append(t)
    MARG = pd.concat(marg, ignore_index=True)

    # headline = best by SELECTION period; show validation untouched
    top = best_sel.iloc[0]
    print("\nBEST by selection period (2022-2024), validated on 2025+:")
    print(top[["combo", "trades", "final_equity", "CAGR_pct", "max_DD_pct", "selection_ret_pct", "validation_ret_pct"]].to_string())
    cands = list(dict.fromkeys(list(best_sel.combo.head(5)) + list(best_cagr.combo.head(3))))

    # re-run chosen combos for trade logs / monthly
    P0 = None
    logs, monthly, yearly = {}, {}, {}
    for key in cands:
        row = G[G.combo == key].iloc[0]
        P = by_mult[row.atr_mult]
        S = P[(P.dist200 <= -row.dist_below_ema200_pct / 100) & (P.days_since_s2 <= row.stage2_within_bars)]
        rc = None if row.r_cap == "raw" else float(row.r_cap)
        r = simulate(S, int(row.max_per_ticker), float(row.gross_cap_x), rc)
        d = r["daily"]
        m = d.resample("ME").last()
        monthly[key] = (m / m.shift(1).fillna(C.ACCOUNT_START) - 1) * 100
        ye = d.resample("YE").last()
        yearly[key] = ((ye / ye.shift(1).fillna(C.ACCOUNT_START) - 1) * 100).rename(lambda x: x.year)
        logs[key] = r["trades"]
    MON = pd.DataFrame(monthly)
    MON.index = MON.index.strftime("%Y-%m")
    MON.index.name = "month"
    YR = pd.DataFrame(yearly).T
    YR.index.name = "combo"
    EQ = pd.DataFrame({k: curves[k].resample("ME").last() for k in cands})
    EQ.index = EQ.index.strftime("%Y-%m")
    EQ.index.name = "month"

    # monthly table incl. trades per month for the headline combo
    tr = logs[top.combo]
    cnt = tr.groupby(tr.entry_date.dt.strftime("%Y-%m")).size().rename("headline_trades_opened")
    MON2 = MON.join(cnt).fillna({"headline_trades_opened": 0})

    cfg = pd.DataFrame({"param": ["account_start", "risk_pct_per_trade", "max_positions_total", "max_position_value_pct",
                                  "commission/swap/slippage", "min_price", "priority", "selection/validation split",
                                  "ranking basis", "equity basis", "gross cap note"],
                        "value": [C.ACCOUNT_START, C.RISK_PCT, C.MAX_POSITIONS, C.MAX_POS_VALUE_PCT,
                                  "NONE (raw spread; user request)", C.PORT_MIN_PRICE, "deepest below EMA200 first",
                                  f"selection < {C.M_SPLIT} <= validation", "R cap 10, gross cap 5x unless stated",
                                  "realized (open P&L not marked)", "5x ~ FCA 1:5; 9x ~ IC Markets limit seen on demo"]})
    idx = pd.DataFrame({"sheet": ["Best_By_Selection", "Best_By_CAGR", "Best_By_Calmar", "Marginal_Effects",
                                  "Monthly_Returns", "Yearly_Returns", "Equity_Monthly", "Grid_All"]
                                 + [f"Trades_{i + 1}" for i in range(len(cands))] + ["Config"],
                        "content": ["top 25 ranked on 2022-2024 return only; validation_ret_pct = untouched 2025+ result",
                                    "top 25 by full-period CAGR (R cap 10)", "top 25 by CAGR / max drawdown (>=150 trades)",
                                    "mean outcome by each parameter value (shows which knobs matter)",
                                    "portfolio return % per calendar month, candidate combos side by side",
                                    "calendar-year return %", "month-end equity (USD)", "every scenario"]
                                   + [f"full trade log: {c}" for c in cands] + ["parameters"]})
    out = C.OUT / "Multi_Entry_Optimisation.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        idx.to_excel(xw, sheet_name="Index", index=False)
        best_sel.to_excel(xw, sheet_name="Best_By_Selection", index=False)
        best_cagr.to_excel(xw, sheet_name="Best_By_CAGR", index=False)
        best_calmar.to_excel(xw, sheet_name="Best_By_Calmar", index=False)
        MARG.to_excel(xw, sheet_name="Marginal_Effects", index=False)
        MON2.reset_index().to_excel(xw, sheet_name="Monthly_Returns", index=False)
        YR.reset_index().to_excel(xw, sheet_name="Yearly_Returns", index=False)
        EQ.reset_index().to_excel(xw, sheet_name="Equity_Monthly", index=False)
        G.sort_values("CAGR_pct", ascending=False).to_excel(xw, sheet_name="Grid_All", index=False)
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
        ws.add_chart(ch, "J2")
    L.style_workbook(out)
    print(f"[OK] {out}")
    print("\nTop 10 by selection period:")
    print(best_sel[["combo", "trades", "CAGR_pct", "max_DD_pct", "selection_ret_pct", "validation_ret_pct", "peak_positions"]].head(10).round(1).to_string(index=False))
    print("\nTop 10 by CAGR:")
    print(best_cagr[["combo", "trades", "CAGR_pct", "max_DD_pct", "selection_ret_pct", "validation_ret_pct"]].head(10).round(1).to_string(index=False))


if __name__ == "__main__":
    main()
