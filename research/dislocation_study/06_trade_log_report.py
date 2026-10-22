# research/dislocation_study/06_trade_log_report.py
"""
STEP 6 -- Full trade log + portfolio summary workbook ($100k, 1% risk, 75 cap).

For each spec in study_config.REPORT_SPECS:
  Trades_<spec>  : every simulated trade (signal/entry/exit dates & prices, stop, shares, notional, $ risk,
                   exit reason, hold days, net R, $ P&L, equity before/after, dist200, days since Stage 2)
Summary          : all portfolio metrics (metrics x scenario columns): 1% risk, 75 cap, slippage 0 / 0.10R, R raw / capped 10
Yearly, Monthly  : return tables.  ExitReasons: exit mix per spec.  Config.
Output: output/Trade_Log_and_Summary.xlsx          Run: python 06_trade_log_report.py
"""
from __future__ import annotations

import importlib
import itertools

import numpy as np
import pandas as pd

import lib_common as L
import study_config as C

sim = importlib.import_module("05_portfolio_sim")


def extras(tr: pd.DataFrame, daily: pd.Series) -> dict:
    n = len(tr)
    w, l = tr[tr.pnl_usd > 0], tr[tr.pnl_usd <= 0]
    streak = best = 0
    for v in (tr.sort_values("exit_date").pnl_usd <= 0):
        streak = streak + 1 if v else 0
        best = max(best, streak)
    return {
        "avg_win_usd": w.pnl_usd.mean(), "avg_loss_usd": l.pnl_usd.mean(),
        "avg_win_R": w.net_r.mean(), "avg_loss_R": l.net_r.mean(),
        "expectancy_usd": tr.pnl_usd.mean(), "best_trade_usd": tr.pnl_usd.max(), "worst_trade_usd": tr.pnl_usd.min(),
        "avg_hold_days": tr.hold_days.mean(), "median_hold_days": tr.hold_days.median(),
        "longest_losing_streak": best,
        "trades_per_year": n / ((daily.index[-1] - daily.index[0]).days / 365.25),
        "start_date": daily.index[0].date(), "end_date": daily.index[-1].date(),
    }


def main() -> None:
    T0 = L.load_all_signals(closed_only=False)
    cols, yearly, monthly, exitmix, logs = {}, {}, {}, [], {}
    for name in C.REPORT_SPECS:
        S = T0[L.spec_mask(T0, C.SPECS[name]) & (T0.entry.values >= C.PORT_MIN_PRICE)]
        for slip, rcap in itertools.product(C.REPORT_SLIPS, [None, 10.0]):
            r = sim.simulate(S, "deepest", C.REPORT_GROSS, slip, rcap)
            tr = r["trades"].sort_values("exit_date").reset_index(drop=True)
            tr["equity_after"] = C.ACCOUNT_START + tr.pnl_usd.cumsum()      # approx: sum of $ P&L in exit order
            key = f"{name} | slip {slip:.2f}R | R {'raw' if rcap is None else 'cap ' + str(int(rcap))}"
            cols[key] = {**r["res"], **extras(tr, r["daily"])}
            yearly[key] = r["yearly"]
            monthly[key] = (r["daily"].resample("ME").last().pct_change() * 100).round(2)
            if slip == C.REPORT_LOG["slip"] and rcap == C.REPORT_LOG["r_cap"]:
                logs[name] = tr
                em = tr.groupby("exit_reason").agg(trades=("pnl_usd", "size"), total_usd=("pnl_usd", "sum"),
                                                   avg_R=("net_r", "mean")).reset_index()
                em.insert(0, "spec", name)
                exitmix.append(em)
        print(f"  done {name}", flush=True)

    SUM = pd.DataFrame(cols)
    SUM.index.name = "metric"
    SUM = SUM.reset_index()
    cfg = pd.DataFrame({"param": ["account_start", "risk_pct_per_trade", "max_positions", "max_position_value_pct",
                                  "gross_cap_x", "commission_per_share_RT", "swap_R_per_trade", "min_price",
                                  "priority", "trade_log_scenario", "equity_basis"],
                        "value": [C.ACCOUNT_START, C.RISK_PCT, C.MAX_POSITIONS, C.MAX_POS_VALUE_PCT, C.REPORT_GROSS,
                                  C.COMMISSION_PER_SHARE_RT, C.SWAP_R_PER_TRADE, C.PORT_MIN_PRICE, "deepest first",
                                  str(C.REPORT_LOG), "realized (open P&L not marked); trades open at last bar valued at last close"]})
    idx = pd.DataFrame({"sheet": ["Summary", "Yearly", "Monthly", "ExitReasons"] + [f"Trades_{n}" for n in logs] + ["Config"],
                        "content": ["portfolio metrics, 1% risk / 75 cap, per spec x slippage x R-cap",
                                    "calendar-year return % per scenario", "month-end equity change % per scenario",
                                    "exit-reason mix & P&L (trade-log scenario)"]
                                   + [f"every trade, spec {n}, scenario {C.REPORT_LOG}" for n in logs] + ["parameters"]})
    Y = pd.DataFrame(yearly).T.round(2)
    Y.index.name = "scenario"
    M = pd.DataFrame(monthly)
    M.index = M.index.strftime("%Y-%m")
    out = C.OUT / "Trade_Log_and_Summary.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        idx.to_excel(xw, sheet_name="Index", index=False)
        SUM.to_excel(xw, sheet_name="Summary", index=False)
        Y.reset_index().to_excel(xw, sheet_name="Yearly", index=False)
        M.reset_index().rename(columns={"index": "month"}).to_excel(xw, sheet_name="Monthly", index=False)
        pd.concat(exitmix).to_excel(xw, sheet_name="ExitReasons", index=False)
        for n, tr in logs.items():
            t = tr.copy()
            for c in ("signal_date", "entry_date", "exit_date"):
                t[c] = pd.to_datetime(t[c]).dt.date
            t.insert(0, "trade_no", range(1, len(t) + 1))
            t["pnl_pct_of_equity"] = t.pnl_usd / t.equity_at_entry * 100
            t.to_excel(xw, sheet_name=f"Trades_{n}"[:31], index=False)
        cfg.to_excel(xw, sheet_name="Config", index=False)
    L.style_workbook(out)
    print(f"[OK] {out}")


if __name__ == "__main__":
    main()
