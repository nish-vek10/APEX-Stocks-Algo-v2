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
    os.environ["DESK_FAKE_NOW"] = "2026-04-16T13:00:00+00:00"          # after cut-off
    assert c.get("/api/status").json()["window_open"] is False
    print("SMOKE TEST OK")


if __name__ == "__main__":
    main()
