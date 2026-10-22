# apex_desk/tests/smoke_test.py
"""Offline smoke test: temp DB, 150-ticker scan replay, API decision + batch flow, dry-run executor (SimBroker).
Run:  python tests/smoke_test.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
tmp = tempfile.mkdtemp()
os.environ["DESK_DB"] = str(Path(tmp) / "t.db")
os.environ["DESK_SCAN_LIMIT"] = "150"
os.environ["DESK_FAKE_NOW"] = "2026-04-16T10:50:00+00:00"

from fastapi.testclient import TestClient  # noqa: E402

from desk import brokers, db, engine  # noqa: E402
from desk.api import app  # noqa: E402
from desk.executor import Executor  # noqa: E402


def main() -> None:
    r = engine.run_scan(workers=1, asof="2026-04-15")
    assert r["scanned"] > 50, r
    c = TestClient(app)
    st = c.get("/api/status").json()
    assert st["window_open"] and st["cutoff_london"].endswith("13:30"), st
    # force one Tier A signal so the test is deterministic on a small subset
    row = db.one("SELECT * FROM signals ORDER BY id LIMIT 1")
    if row:
        db.x("UPDATE signals SET tier='A' WHERE id=?", (row["id"],))
        assert c.post(f"/api/signals/{row['id']}/decision", json={"decision": "trade", "by": "Test"}).status_code == 200
        assert c.post("/api/batch/confirm", json={"by": "Test"}).status_code == 200
        ex = Executor(brokers.SimBroker())
        ex.c["timing"]["no_quote_retry_window_sec"] = 0
        out = ex.run_session(force=True)
        assert out["executed"] + len(out["failed"]) == 1, out
    # --- v2: risk override, star, rollover to Watchlist, live refresh ---
    rows = c.get("/api/signals?session=2026-04-16").json()
    tr = [r for r in rows if r["exec_status"] == "none" and r["tier"] in ("A", "B", "W")]
    info = [r for r in rows if r["tier"] == "C"]
    if tr:
        t0 = tr[0]
        base = t0["est_risk"]
        assert c.post(f"/api/signals/{t0['id']}/decision", json={"decision": "skip", "by": "Test"}).status_code == 200
        j = c.post(f"/api/signals/{t0['id']}/risk", json={"risk_pct": 0.015, "by": "Test"}).json()
        assert abs(j["risk_pct"] - 0.015) < 1e-9 and j["est_risk"] >= base * 0.9, j
        assert c.post(f"/api/signals/{t0['id']}/risk", json={"risk_pct": 0.5, "by": "Test"}).status_code == 400
    if info:
        i0 = info[0]
        assert c.post(f"/api/signals/{i0['id']}/decision", json={"decision": "trade", "by": "T"}).status_code == 403
        assert c.post(f"/api/signals/{i0['id']}/watch", json={"by": "Test"}).status_code == 200
        assert c.post(f"/api/signals/{i0['id']}/decision", json={"decision": "trade", "by": "T"}).status_code == 200
    os.environ["DESK_FAKE_NOW"] = "2026-04-16T20:00:00+00:00"          # after the open: session rolls to 2026-04-17
    st = c.get("/api/status").json()
    assert st["session_date"] == "2026-04-17", st["session_date"]
    rolled = c.get("/api/signals?session=2026-04-17").json()
    if tr:
        r0 = next(r for r in rolled if r["id"] == tr[0]["id"])
        assert r0["decision"] == "none" and r0["last_decision"] == "skip" and r0["last_decision_date"] == "2026-04-16", r0
        assert abs(r0["risk_pct"] - 0.015) < 1e-9
    r2 = engine.run_scan(workers=1, asof="2026-04-16")          # new batch: previous setups become watchlist, metrics refreshed
    assert r2["signal_date"] == "2026-04-16", r2
    ch = c.get("/api/chart/AAPL").json() if False else None
    os.environ["DESK_FAKE_NOW"] = "2026-04-17T13:00:00+00:00"          # after cut-off
    assert c.get("/api/status").json()["window_open"] is False
    print("SMOKE TEST OK")


if __name__ == "__main__":
    main()
