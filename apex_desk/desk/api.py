# apex_desk/desk/api.py
"""FastAPI app: JSON API + static dashboard. Run:  python -m desk serve"""
from __future__ import annotations

import threading
from functools import lru_cache
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from desk import clock, db, engine, rules, settings

app = FastAPI(title="APEX Desk", version="1.0.0")
WEB = settings.DESK_ROOT / "web"
_scan_lock = threading.Lock()
_scan_state: Dict[str, Any] = {"running": False}


class DecisionIn(BaseModel):
    decision: str
    by: str = "Nish"
    note: str = ""


class RiskIn(BaseModel):
    risk_pct: float          # fraction of equity, e.g. 0.01 = 1%
    by: str = "Nish"


class WatchIn(BaseModel):
    by: str = "Nish"


class BatchIn(BaseModel):
    by: str = "Nish"


@app.get("/api/status")
def status() -> Dict[str, Any]:
    rules.expire_stale()
    c = settings.cfg()
    ck = clock.status()
    sess = ck["session_date"]
    rows = db.q("SELECT tier, decision, exec_status, COALESCE(watch,0) watch, COUNT(*) n FROM signals WHERE entry_date=? "
                "GROUP BY tier, decision, exec_status, COALESCE(watch,0)", (sess,))
    trad = set(rules.tradable_tiers())
    a = [r for r in rows if r["tier"] in trad or r["watch"]]
    tot = lambda f: sum(r["n"] for r in a if f(r))  # noqa: E731
    by_tier = {}
    for r in rows:
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + r["n"]
    ex = rules.exposure()
    return {
        **ck, "mode": c["mode"], "name": c["desk"]["name"], "reviewers": c["desk"]["review_by"],
        "equity": rules.equity(), "max_positions": c["risk"]["max_positions"], "max_per_ticker": c["risk"]["max_per_ticker"],
        "risk_pct": c["risk"]["risk_pct"], "max_risk_pct": c["risk"].get("max_risk_pct_override", 0.02),
        "manual_risk_mult": c["strategy"].get("manual_risk_mult", 0.5),
        "board_date": (db.kv_get("last_scan") or {}).get("signal_date"), "atr_multiplier": c["strategy"]["atr_multiplier"],
        "tiers": c["strategy"]["tiers"], "setup_info": c["strategy"]["setup_info"], "max_gross_x": c["risk"].get("max_gross_x"),
        "counts": {"tradable": tot(lambda r: True), "by_tier": by_tier, "trade": tot(lambda r: r["decision"] == "trade"),
                   "skip": tot(lambda r: r["decision"] == "skip"), "undecided": tot(lambda r: r["decision"] == "none"),
                   "queued": tot(lambda r: r["exec_status"] == "queued"),
                   "executed": tot(lambda r: r["exec_status"] == "executed"),
                   },
        "exposure": ex, "last_scan": db.kv_get("last_scan"), "executor": db.kv_get("executor_snapshot"),
        "scan_running": _scan_state["running"], "batch": rules.batch_preview(sess),
    }


@app.get("/api/signals")
def signals(session: Optional[str] = None, tier: Optional[str] = None) -> Any:
    sess = session or clock.status()["session_date"]
    rows = db.q("SELECT * FROM signals WHERE entry_date=? ORDER BY tier ASC, dist200 ASC", (sess,))
    if tier:
        want = set(tier.split(","))
        rows = [r for r in rows if r["tier"] in want]
    out = []
    for r in rows:
        r["open_same_ticker"] = rules.per_ticker_open(r["ticker"])
        r["locked_reason"] = rules.decision_allowed(r)
        out.append(r)
    return out


@app.get("/api/chart/{ticker}")
def chart(ticker: str, asof: Optional[str] = None, sid: Optional[str] = None) -> Any:
    return _chart(ticker.upper(), asof)


@lru_cache(maxsize=256)
def _chart_cached(ticker: str, asof: Optional[str], stamp: str) -> Dict[str, Any]:
    return engine.chart_payload(ticker, asof)


def _chart(ticker: str, asof: Optional[str]) -> Dict[str, Any]:
    try:
        p = settings.cache_dir() / f"{ticker}.parquet"
        stamp = str(int(p.stat().st_mtime)) if p.exists() else "0"
        return _chart_cached(ticker, asof, stamp)
    except FileNotFoundError:
        raise HTTPException(404, f"no cached data for {ticker}")


@app.post("/api/signals/{sig_id}/decision")
def decide(sig_id: int, body: DecisionIn) -> Any:
    try:
        return rules.set_decision(sig_id, body.decision, body.by, body.note)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/signals/{sig_id}/risk")
def risk(sig_id: int, body: RiskIn) -> Any:
    try:
        return rules.set_risk(sig_id, body.risk_pct, body.by)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/signals/{sig_id}/watch")
def watch(sig_id: int, body: WatchIn) -> Any:
    try:
        return rules.set_watch(sig_id, body.by)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))


@app.get("/api/batch/preview")
def batch_preview() -> Any:
    return rules.batch_preview(clock.status()["session_date"])


@app.post("/api/batch/confirm")
def batch_confirm(body: BatchIn) -> Any:
    try:
        return rules.lock_batch(clock.status()["session_date"], body.by)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/batch/unlock")
def batch_unlock(body: BatchIn) -> Any:
    try:
        return {"unlocked": rules.unlock_batch(clock.status()["session_date"], body.by)}
    except PermissionError as e:
        raise HTTPException(403, str(e))


@app.post("/api/scan")
def scan() -> Any:
    if _scan_state["running"]:
        return {"started": False, "reason": "scan already running"}

    def work() -> None:
        with _scan_lock:
            _scan_state["running"] = True
            try:
                engine.run_scan()
            finally:
                _scan_state["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"started": True}


@app.get("/api/positions")
def positions(status: str = "open") -> Any:
    order = "exit_date DESC, ticker" if status == "closed" else "entry_date DESC, ticker"
    return db.q(f"SELECT * FROM positions WHERE status=? ORDER BY {order}", (status,))


@app.get("/api/executions")
def executions(limit: int = Query(100, le=500)) -> Any:
    return db.q("SELECT * FROM executions ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/audit")
def audit(limit: int = Query(100, le=500)) -> Any:
    return db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/history")
def history(limit: int = Query(300, le=1000)) -> Any:
    return db.q("SELECT id, signal_date, entry_date, ticker, tier, setup, decision, decided_by, exec_status, dist200, days_since_s2, "
                "watch, last_decision, last_decision_date, risk_pct FROM signals WHERE tier IN ('A','B','W') OR watch=1 "
                "ORDER BY signal_date DESC, ticker LIMIT ?", (limit,))


app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


@app.get("/")
def index() -> Any:
    return FileResponse(str(WEB / "index.html"))
