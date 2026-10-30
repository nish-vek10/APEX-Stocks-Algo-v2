# apex_desk/tests/pnl_test.py
"""Daily P&L snapshot test: backfill, idempotency, frozen history, exit day, repeated entries. Run from apex_desk/:  python tests/pnl_test.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["DESK_DB"] = str(Path(tempfile.mkdtemp()) / "t.db")

import pandas as pd  # noqa: E402

from desk import db, engine, pnl  # noqa: E402

DAYS = pd.to_datetime(["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"])
PX = {"AAA": [10, 11, 12, 11, 13], "BBB": [20, 19, 18, 21, 22]}
engine._read = lambda t: pd.DataFrame({"date": DAYS, "close": PX[t]}) if t in PX else None      # type: ignore[assignment]

ins = ("INSERT INTO positions(ticker,entry_date,entry_price,stop_price,shares,stop_distance,status,exit_date,exit_price,pnl_total) "
       "VALUES(?,?,?,?,?,?,?,?,?,?)")
db.x(ins, ("AAA", "2026-10-26", 10.0, 9.0, 100, 1.0, "open", None, None, None))                  # position 1
db.x(ins, ("AAA", "2026-10-28", 12.0, 11.0, 50, 1.0, "open", None, None, None))                  # position 2: same ticker entered again
db.x(ins, ("BBB", "2026-10-27", 20.0, 18.5, 10, 1.5, "closed", "2026-10-29", 20.5, 5.0))         # position 3: exited 29th

r = pnl.rebuild()
d = {x["date"]: x for x in pnl.daily()}
assert len(d) == 5, d
assert d["2026-10-26"]["pnl"] == 0 and d["2026-10-26"]["n_opened"] == 1 and d["2026-10-26"]["risk_opened"] == 100
assert d["2026-10-27"]["pnl"] == 100 + (-10) and d["2026-10-27"]["n_open"] == 2
assert d["2026-10-28"]["pnl"] == 200 + 0 + (-20) and d["2026-10-28"]["n_opened"] == 1 and d["2026-10-28"]["risk_opened"] == 50
assert d["2026-10-29"]["pnl"] == 100 + (-50) + 5.0 and d["2026-10-29"]["n_open"] == 3      # exit-day row holds the realised P&L
assert d["2026-10-30"]["pnl"] == 300 + 50 and d["2026-10-30"]["n_open"] == 2 and d["2026-10-30"]["risk_open"] == 150
rows = [x for x in pnl.per_position() if x["position_id"] == 3]
assert [x["status"] for x in rows] == ["exit", "open", "open"] and rows[0]["close"] == 20.5, rows
before = pnl.per_position()
pnl.rebuild()
assert pnl.per_position() == before, "rebuild must be idempotent"
PX["AAA"][1] = 99                                                                         # a later cache adjustment must not rewrite history
pnl.rebuild()
assert [x for x in pnl.per_position() if x["position_id"] == 1 and x["date"] == "2026-10-27"][0]["close"] == 11
db.x("UPDATE positions SET status='closed', exit_date='2026-10-28', exit_price=12, pnl_total=200 WHERE id=1")   # late-detected broker close
pnl.rebuild()
assert max(x["date"] for x in pnl.per_position() if x["position_id"] == 1) == "2026-10-28"
print("pnl_test OK", r)
