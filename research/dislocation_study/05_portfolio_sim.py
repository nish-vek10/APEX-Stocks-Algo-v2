# research/dislocation_study/05_portfolio_sim.py
"""
STEP 5 -- Account-level portfolio simulation ($100k, 1% risk, 75-position cap).

Mirrors production rules:
  * size   : shares = floor(equity * RISK_PCT / stop_dist)   (equity = realized equity at entry)
  * cap    : notional <= MAX_POS_VALUE_PCT * equity  (risk.yaml max_position_pct)
  * slots  : at most MAX_POSITIONS open; one position per ticker; exits processed before entries
  * costs  : $0.04/share round trip + swap (R) + optional extra slippage (R)
  * order  : same-day candidates ranked by PRIORITY ('alpha' = today's production, 'deepest' = most below EMA200)
  * gross  : optional cap on total notional / equity (5.0 = FCA retail 1:5)
Signals still open at the last bar are valued at that bar's close (flag in output).
Equity is REALIZED equity (open P&L not marked) -> drawdown is slightly understated; peaks of
open risk and gross exposure are reported so you can see the unrealized-risk envelope.

Output: output/Portfolio_Sim_Report.xlsx
Run:    python 05_portfolio_sim.py
"""
from __future__ import annotations

import heapq
import itertools

import numpy as np
import pandas as pd
from openpyxl.chart import LineChart, Reference

import lib_common as L
import study_config as C


def simulate(T: pd.DataFrame, priority: str, gross_cap, slip: float, r_cap=None, risk_pct: float = C.RISK_PCT) -> dict:
    T = T.copy()
    T["net_r"] = L.net_r(T, slip)
    if r_cap is not None:
        T["net_r"] = T.net_r.clip(upper=r_cap)
    T = T.sort_values(["entry_date", "ticker"] if priority == "alpha" else ["entry_date", "dist200"]).reset_index(drop=True)
    eq = C.ACCOUNT_START
    open_h: list = []                 # (exit_date, seq, ticker, pnl, notional, risk)
    open_tk: set = set()
    gross = risk_open = 0.0
    peak_pos = 0
    peak_gross = peak_risk = 0.0
    skipped = {"cap": 0, "ticker_open": 0, "gross": 0, "size0": 0}
    taken_rows, eq_pts = [], []       # eq_pts: (date, equity after exit)
    seq = 0

    def close_until(d):
        nonlocal eq, gross, risk_open
        while open_h and open_h[0][0] <= d:
            xd, _, tk, pnl, notional, rsk = heapq.heappop(open_h)
            eq += pnl
            gross -= notional
            risk_open -= rsk
            open_tk.discard(tk)
            eq_pts.append((xd, eq))

    entry_d = T.entry_date.values
    for dte, g in T.groupby("entry_date", sort=True):
        close_until(dte)
        for row in g.itertuples(index=False):
            if eq <= 0:
                skipped["size0"] += 1
                continue
            if row.ticker in open_tk:
                skipped["ticker_open"] += 1
                continue
            if len(open_h) >= C.MAX_POSITIONS:
                skipped["cap"] += 1
                continue
            sd = row.stop_dist
            if not (sd > 0) or not (row.entry > 0):
                skipped["size0"] += 1
                continue
            shares = int(eq * risk_pct / sd)
            shares = min(shares, int(eq * C.MAX_POS_VALUE_PCT / row.entry))
            if shares < 1:
                skipped["size0"] += 1
                continue
            notional = shares * row.entry
            if gross_cap is not None and (gross + notional) > gross_cap * eq:
                skipped["gross"] += 1
                continue
            rsk = shares * sd
            pnl = shares * sd * row.net_r              # net_r = R net of commission, swap, slippage
            seq += 1
            heapq.heappush(open_h, (row.exit_date, seq, row.ticker, pnl, notional, rsk))
            open_tk.add(row.ticker)
            gross += notional
            risk_open += rsk
            peak_pos = max(peak_pos, len(open_h))
            peak_gross = max(peak_gross, gross / eq)
            peak_risk = max(peak_risk, risk_open / eq)
            taken_rows.append((row.ticker, row.signal_date, dte, row.entry, row.stop_price, shares, notional, rsk,
                               row.exit_date, row.exit_price, row.exit_reason, row.hold, pnl, row.net_r,
                               row.open_at_end, eq, row.dist200, row.days_since_s2, row.stage, row.prev_stage))
    close_until(pd.Timestamp.max)

    tr = pd.DataFrame(taken_rows, columns=[
        "ticker", "signal_date", "entry_date", "entry_price", "stop_price", "shares", "notional", "risk_usd",
        "exit_date", "exit_price", "exit_reason", "hold_days", "pnl_usd", "net_r", "open_at_end", "equity_at_entry",
        "dist200", "days_since_s2", "stage", "prev_stage"])
    ec = pd.DataFrame(eq_pts, columns=["date", "equity"])
    ec = ec.groupby("date").equity.last()
    daily = ec.reindex(pd.date_range(ec.index.min(), ec.index.max(), freq="B")).ffill()
    daily.iloc[0] = daily.iloc[0] if not np.isnan(daily.iloc[0]) else C.ACCOUNT_START
    peak = daily.cummax()
    dd = (daily / peak - 1)
    ret = daily.pct_change().dropna()
    yrs = (daily.index[-1] - pd.Timestamp(C.PORT_START)).days / 365.25
    final = float(daily.iloc[-1])
    n = len(tr)
    pos, neg = tr.pnl_usd[tr.pnl_usd > 0].sum(), -tr.pnl_usd[tr.pnl_usd < 0].sum()
    res = {
        "trades": n, "win_pct": float((tr.pnl_usd > 0).mean() * 100) if n else np.nan,
        "final_equity": final, "total_return_pct": (final / C.ACCOUNT_START - 1) * 100,
        "CAGR_pct": ((final / C.ACCOUNT_START) ** (1 / yrs) - 1) * 100 if final > 0 else np.nan,
        "max_DD_pct": float(dd.min() * 100),
        "sharpe_daily": float(ret.mean() / ret.std() * np.sqrt(252)) if ret.std() > 0 else np.nan,
        "profit_factor": float(pos / neg) if neg > 0 else np.nan,
        "avg_net_R": float(tr.net_r.clip(upper=C.WINSOR_R).mean()) if n else np.nan,
        "avg_notional_pct_eq": float((tr.notional / tr.equity_at_entry).mean() * 100) if n else np.nan,
        "peak_positions": peak_pos, "peak_gross_x": peak_gross, "peak_open_risk_pct": peak_risk * 100,
        "skipped_cap": skipped["cap"], "skipped_gross": skipped["gross"],
        "skipped_ticker_open": skipped["ticker_open"], "skipped_size0": skipped["size0"],
        "open_at_end_trades": int(tr.open_at_end.sum()) if n else 0,
    }
    yr = daily.groupby(daily.index.year).agg(["first", "last"])
    prev = C.ACCOUNT_START
    yret = {}
    for y, r in yr.iterrows():
        yret[int(y)] = (r["last"] / prev - 1) * 100
        prev = r["last"]
    return {"res": res, "yearly": yret, "daily": daily, "dd": dd, "trades": tr}


def main() -> None:
    T0 = L.load_all_signals(closed_only=False)
    print(f"[INFO] candidate signals from {C.PORT_START}: {len(T0):,}; adv20 coverage {T0.adv20.notna().mean():.1%}")
    rows, yearly, curves = [], [], {}
    best_tr = None
    for (name, spec), prio, gcap, slip, rcap in itertools.product(C.SPECS.items(), C.PRIORITIES, C.GROSS_CAPS, C.PORT_SLIPPAGE_R, C.R_CAPS):
        S = T0[L.spec_mask(T0, spec) & (T0.entry.values >= C.PORT_MIN_PRICE)]
        if len(S) < 20:
            continue
        r = simulate(S, prio, gcap, slip, rcap)
        key = {"spec": name, "priority": prio, "gross_cap_x": gcap if gcap else "none", "slippage_R": slip, "r_cap": rcap if rcap else "none", "min_price": C.PORT_MIN_PRICE,
               "max_positions": C.MAX_POSITIONS, "risk_pct": C.RISK_PCT * 100}
        rows.append({**key, **r["res"]})
        yearly.append({**key, **{f"ret_{y}_pct": v for y, v in r["yearly"].items()}})
        h = C.HEADLINE
        if prio == h["priority"] and gcap == h["gross_cap"] and slip == h["slip"] and rcap == h["r_cap"]:
            curves[name] = r["daily"].resample("ME").last()
            if name == "D25_S40":
                best_tr = r["trades"]
        print(f"  {name:18s} {prio:8s} gross={str(gcap):5s} slip={slip:.2f} rcap={str(rcap):4s} -> "
              f"trades {r['res']['trades']:6d}  final ${r['res']['final_equity']:>12,.0f}  "
              f"CAGR {r['res']['CAGR_pct']:6.1f}%  maxDD {r['res']['max_DD_pct']:6.1f}%  "
              f"peakPos {r['res']['peak_positions']}  peakGross {r['res']['peak_gross_x']:.1f}x", flush=True)
    sens = []
    h = C.HEADLINE
    for (name, spec), rp in itertools.product(C.SPECS.items(), C.RISK_SENSITIVITY):
        S = T0[L.spec_mask(T0, spec) & (T0.entry.values >= C.PORT_MIN_PRICE)]
        r = simulate(S, h["priority"], h["gross_cap"], h["slip"], h["r_cap"], rp)
        sens.append({"spec": name, "risk_pct": rp * 100, **{k: r["res"][k] for k in (
            "trades", "final_equity", "CAGR_pct", "max_DD_pct", "sharpe_daily", "peak_positions", "peak_open_risk_pct")}})
    SENS = pd.DataFrame(sens)
    R = pd.DataFrame(rows)
    Y = pd.DataFrame(yearly)
    EQ = pd.DataFrame(curves)
    EQ.index = EQ.index.strftime("%Y-%m")
    EQ = EQ.reset_index().rename(columns={"index": "month", "date": "month"})

    idx = pd.DataFrame({"sheet": ["Scenarios", "Yearly_Returns", "Equity_Monthly", "Sample_Trades", "Risk_Sensitivity", "Config"],
                        "content": ["every scenario: spec x same-day priority x gross cap x slippage",
                                    "calendar-year return % per scenario",
                                    f"month-end realized equity, headline scenario {C.HEADLINE}",
                                    "trade list of spec D25_S40, headline scenario",
                                    "risk per trade 0.25-1.0% under the headline scenario",
                                    "account + risk parameters"]})
    cfg = pd.DataFrame({"param": ["account_start", "risk_pct_per_trade", "max_positions", "max_position_value_pct",
                                  "commission_per_share_RT", "swap_R_per_trade", "port_start", "equity_basis", "min_price_floor"],
                        "value": [C.ACCOUNT_START, C.RISK_PCT, C.MAX_POSITIONS, C.MAX_POS_VALUE_PCT,
                                  C.COMMISSION_PER_SHARE_RT, C.SWAP_R_PER_TRADE, C.PORT_START,
                                  "realized equity (open P&L not marked)", C.PORT_MIN_PRICE]})
    out = C.OUT / "Portfolio_Sim_Report.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        idx.to_excel(xw, sheet_name="Index", index=False)
        R.to_excel(xw, sheet_name="Scenarios", index=False)
        Y.to_excel(xw, sheet_name="Yearly_Returns", index=False)
        EQ.to_excel(xw, sheet_name="Equity_Monthly", index=False)
        (best_tr if best_tr is not None else pd.DataFrame()).to_excel(xw, sheet_name="Sample_Trades", index=False)
        SENS.to_excel(xw, sheet_name="Risk_Sensitivity", index=False)
        cfg.to_excel(xw, sheet_name="Config", index=False)
        ws = xw.sheets["Equity_Monthly"]
        if len(EQ) > 1:
            ch = LineChart()
            ch.title = f"Month-end equity (USD) - {C.HEADLINE}"
            ch.height, ch.width = 11, 26
            ch.add_data(Reference(ws, min_col=2, max_col=EQ.shape[1], min_row=1, max_row=len(EQ) + 1), titles_from_data=True)
            ch.set_categories(Reference(ws, min_col=1, min_row=2, max_row=len(EQ) + 1))
            ws.add_chart(ch, "J2")
    L.style_workbook(out)
    print(f"[OK] {out}")


if __name__ == "__main__":
    main()
