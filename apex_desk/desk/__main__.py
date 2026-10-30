# apex_desk/desk/__main__.py
"""CLI:  python -m desk <serve|scan|execute|snapshot|scheduler|status|seed-demo>"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from desk import settings


def main() -> None:
    ap = argparse.ArgumentParser(prog="desk")
    ap.add_argument("cmd", choices=["serve", "scan", "execute", "snapshot", "scheduler", "status", "sync", "requeue", "force-queue", "pnl"])
    ap.add_argument("tickers", nargs="*", help="force-queue: tickers to queue for today's open, e.g. CCB")
    ap.add_argument("--risk", type=float, default=None, help="force-queue: risk %% of equity override, e.g. 0.5")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--workers", type=int, default=None, help="scan worker processes")
    ap.add_argument("--asof", default=None, help="scan: replay a past date (demo/testing), e.g. 2026-04-07")
    ap.add_argument("--allow-stale", action="store_true", help="scan: accept an old cache (testing only)")
    ap.add_argument("--loop", action="store_true", help="sync: run continuously")
    ap.add_argument("--as-session", action="store_true", help="scan: TEST ONLY, relabel stale signals as today's session")
    ap.add_argument("--force", action="store_true", help="execute: run even on a non-trading day / outside the open")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(settings.log_dir() / "desk.log", encoding="utf-8")])
    if a.cmd == "serve":
        import uvicorn
        print(f"APEX Desk -> http://{a.host}:{a.port}   mode={settings.cfg()['mode']}")
        uvicorn.run("desk.api:app", host=a.host, port=a.port, log_level="warning")
    elif a.cmd == "scan":
        from desk import engine
        print(json.dumps(engine.run_scan(workers=a.workers, allow_stale=a.allow_stale, asof=a.asof, as_session=a.as_session), indent=2, default=str))
    elif a.cmd == "execute":
        from desk.executor import Executor
        print(json.dumps(Executor().run_session(force=a.force), indent=2, default=str))
    elif a.cmd == "pnl":
        from desk import pnl
        print(json.dumps(pnl.rebuild(), indent=2, default=str))
    elif a.cmd == "snapshot":
        from desk.scheduler import job_snapshot
        job_snapshot()
        from desk import db
        print(json.dumps(db.kv_get("executor_snapshot"), indent=2))
    elif a.cmd == "scheduler":
        from desk.scheduler import main as run
        run()
    elif a.cmd == "sync":
        from desk import sync
        if not sync.enabled():
            sys.exit("Sync disabled: set sync.enabled: true in conf/desk.yaml and DESK_SUPABASE_URL / DESK_SUPABASE_SERVICE_KEY in .env")
        sync.loop() if a.loop else print(json.dumps(sync.cycle(prune=True), indent=2, default=str))
    elif a.cmd == "requeue":
        from desk import db
        rows = db.q("SELECT id, ticker, entry_date, exec_detail FROM signals WHERE exec_status='failed' AND decision='trade' "
                    "AND (exec_detail LIKE '%null_result%' OR exec_detail LIKE '%IPC%' OR exec_detail LIKE '%no_live_quote%') ORDER BY entry_date DESC")
        for r in rows:
            db.x("UPDATE signals SET exec_status='queued', exec_detail=NULL WHERE id=?", (r["id"],))
        db.audit("system", "requeue", {"n": len(rows), "tickers": [r["ticker"] for r in rows]})
        print(f"Re-queued {len(rows)} failed signals (connection errors only): {[r['ticker'] for r in rows]}")
    elif a.cmd == "force-queue":
        from desk import rules
        if not a.tickers:
            sys.exit("usage: python -m desk force-queue CCB [TICKER ...] [--risk 0.5]")
        for t in a.tickers:
            try:
                s = rules.force_queue(t, by="manual-cli", risk_pct=(a.risk / 100 if a.risk else None))
                print(f"QUEUED {s['ticker']}: tier {s['tier']} | signal {s['signal_date']} | risk {(s['risk_pct'] or 0) * 100:.2f}% "
                      f"| entry_date {s['entry_date']} | est {s['est_shares']} sh, ${s['est_risk']:.0f} risk")
            except (PermissionError, KeyError, ValueError) as e:
                print(f"REFUSED {t}: {e}")
    elif a.cmd == "status":
        from desk import clock
        print(json.dumps(clock.status(), indent=2))


if __name__ == "__main__":
    main()
