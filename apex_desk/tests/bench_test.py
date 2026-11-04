# apex_desk/tests/bench_test.py
"""Benchmark test: day % vs previous close, equal / risk weights, exit day, double entry, S&P leg. Run from apex_desk/:  python tests/bench_test.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["DESK_DB"] = str(Path(tempfile.mkdtemp()) / "t.db")

import pandas as pd  # noqa: E402

from desk import bench, db, engine  # noqa: E402

DAYS = pd.to_datetime(["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"])
PX = {"AAA": [10, 11, 12.1, 12.1, 13.31], "BBB": [20, 20, 18, 21, 21]}
engine._read = lambda t: pd.DataFrame({"date": DAYS, "close": PX[t]}) if t in PX else None      # type: ignore[assignment]
SPX = {"2026-10-23": 5000.0, "2026-10-26": 5050.0, "2026-10-27": 5000.0, "2026-10-28": 5100.0, "2026-10-29": 5100.0, "2026-10-30": 5151.0}
bench.spx_closes = lambda start, refresh=False: dict(SPX)                                                # type: ignore[assignment]
bench.configured = lambda: True                                                            # type: ignore[assignment]

ins = ("INSERT INTO positions(ticker,tier,entry_date,entry_price,shares,stop_distance,status,exit_date,exit_price) VALUES(?,?,?,?,?,?,?,?,?)")
db.x(ins, ("AAA", "A", "2026-10-26", 10.0, 100, 1.0, "open", None, None))                  # weight 1.0 (A)
db.x(ins, ("AAA", "B", "2026-10-28", 12.0, 50, 1.0, "open", None, None))                   # same ticker again: counted once, max multiple
db.x(ins, ("BBB", "B", "2026-10-27", 20.0, 10, 1.5, "closed", "2026-10-29", 21.0))        # weight 0.5 (B), exits 29th at 21 (prev close 18)
from desk import pnl  # noqa: E402

pnl.rebuild()                                                                              # model equity = 100,000 + all P&L since inception
r = bench.rebuild()
d = {x["date"]: x for x in bench.daily()}
s = {(x["date"], x["ticker"]): x for x in bench.stocks()}
assert abs(d["2026-10-27"]["spx_pct"] - (5000 / 5050 - 1)) < 1e-9
assert abs(s[("2026-10-27", "AAA")]["pct"] - 0.10) < 1e-9                                     # 11 / 10 - 1
assert ("2026-10-26", "BBB") not in s and s[("2026-10-27", "BBB")]["pct"] == 0.0              # not held yet / flat day
assert abs(d["2026-10-27"]["ew_pct"] - 0.05) < 1e-9                                            # (10% + 0%) / 2
assert abs(d["2026-10-27"]["w_pct"] - (0.10 * 1.0 + 0.0 * 0.5) / 1.5) < 1e-9                  # risk weighted
assert abs(s[("2026-10-28", "BBB")]["pct"] - (18 / 20 - 1)) < 1e-9 and s[("2026-10-28", "AAA")]["weight"] == 1.0
assert abs(s[("2026-10-29", "BBB")]["pct"] - (21 / 18 - 1)) < 1e-9                             # exit day uses the exit price vs previous close
assert ("2026-10-30", "BBB") not in s and d["2026-10-30"]["n_stocks"] == 1
e = {x["date"]: x for x in pnl.daily()}
assert e["2026-10-27"]["equity"] == 100100.0 and e["2026-10-26"]["equity"] == 100000.0           # 100k + (11-10)*100 + (20-20)*10
assert abs(d["2026-10-27"]["port_pct"] - 0.001) < 1e-9 and d["2026-10-26"]["port_pct"] == 0.0   # first day is measured against the 100k start
assert e["2026-10-30"]["realised"] == 10.0 and e["2026-10-30"]["equity"] == 100000 + 331 + 65.5 + 10 and e["2026-10-29"]["realised"] == 0.0   # BBB realised +10 counts after its exit day
SPX.pop("2026-10-28")                                                                      # a missing close must blank the day AND the next day, never span 2 days
bench.rebuild()
d = {x["date"]: x for x in bench.daily()}
assert d["2026-10-28"]["spx_pct"] is None and d["2026-10-29"]["spx_pct"] is None and d["2026-10-30"]["spx_pct"] is not None
SPX["2026-10-28"] = 5100.0
bench.rebuild()
before = bench.daily(), bench.stocks()
bench.rebuild()
assert (bench.daily(), bench.stocks()) == before, "rebuild must be idempotent"
print("bench_test OK", r)
