# apex_desk/desk/rules.py
"""Business rules shared by the API and the executor: decisions, batch locking, capacity/exposure checks."""
from __future__ import annotations

from typing import Any, Dict, List

from desk import clock, db, engine, settings


def tradable_tiers() -> List[str]:
    return [k for k, v in settings.cfg()["strategy"]["tiers"].items() if v.get("tradable")]


def _in_tiers() -> str:
    return ",".join("'%s'" % t for t in tradable_tiers())


def tradable_sql() -> str:
    """SQL predicate for rows that may be traded: A/B/W tiers plus any starred (watch=1) info setup."""
    return f"(tier IN ({_in_tiers()}) OR watch=1)"


def is_tradable(sig: Dict[str, Any]) -> bool:
    return sig["tier"] in tradable_tiers() or bool(sig.get("watch"))


def equity() -> float:
    return float(db.kv_get("equity", settings.cfg()["risk"]["default_equity"]))


def open_positions() -> List[Dict[str, Any]]:
    return db.q("SELECT * FROM positions WHERE status='open'")


def exposure() -> Dict[str, float]:
    ps = open_positions()
    eq = max(equity(), 1.0)
    notional = sum((p["shares"] or 0) * (p["entry_price"] or 0) for p in ps)
    risk = sum((p["shares"] or 0) * (p["stop_distance"] or 0) for p in ps)
    return {"open_positions": len(ps), "notional": notional, "gross_x": notional / eq, "open_risk": risk,
            "open_risk_pct": risk / eq * 100}


def per_ticker_open(ticker: str) -> int:
    r = db.one("SELECT COUNT(*) n FROM positions WHERE status='open' AND ticker=?", (ticker,))
    return int(r["n"]) if r else 0


def decision_allowed(sig: Dict[str, Any]) -> str:
    """'' if a decision may be changed, else the reason it may not."""
    if not is_tradable(sig):
        return "Info only: star it to make it tradable"
    if sig["exec_status"] not in ("none",):
        return f"Signal already {sig['exec_status']}"
    if not clock.review_started(sig["entry_date"]):
        return "Review is locked until the next signal batch is published"
    if not clock.window_open(sig["entry_date"]):
        return "Cut-off passed: decisions are closed for this session"
    return ""


def set_decision(sig_id: int, decision: str, by: str, note: str = "") -> Dict[str, Any]:
    sig = db.one("SELECT * FROM signals WHERE id=?", (sig_id,))
    if not sig:
        raise KeyError("signal not found")
    why = decision_allowed(sig)
    if why:
        raise PermissionError(why)
    if decision not in ("trade", "skip", "none"):
        raise ValueError("decision must be trade | skip | none")
    db.x("UPDATE signals SET decision=?, decided_by=?, decided_at=?, note=? WHERE id=?",
         (decision, by, db.now_iso(), note, sig_id))
    db.x("INSERT INTO decisions(signal_id,decided_by,decision,note,decided_at) VALUES(?,?,?,?,?)",
         (sig_id, by, decision, note, db.now_iso()))
    db.audit(by, f"decision:{decision}", {"signal_id": sig_id, "ticker": sig["ticker"], "note": note})
    return db.one("SELECT * FROM signals WHERE id=?", (sig_id,))


def batch_preview(entry_date: str) -> Dict[str, Any]:
    """What 'Trade all confirmed' would queue, with exposure checks. Sizing is re-done at execution from the live ask."""
    rk = settings.cfg()["risk"]
    eq = equity()
    sigs = db.q(f"SELECT * FROM signals WHERE entry_date=? AND {tradable_sql()} AND decision='trade' AND exec_status='none' "
                "ORDER BY dist200 ASC", (entry_date,))
    ex = exposure()
    slots = int(rk["max_positions"]) - ex["open_positions"]
    risk = sum(s["est_risk"] or 0 for s in sigs)
    notional = sum(s["est_notional"] or 0 for s in sigs)
    warnings: List[str] = []
    if len(sigs) > slots:
        warnings.append(f"{len(sigs)} confirmed > {slots} free slots: the deepest-below-EMA200 signals fill first, the rest are skipped.")
    gx = rk.get("max_gross_x")
    if gx and (ex["notional"] + notional) / max(eq, 1) > float(gx):
        warnings.append(f"Projected gross exposure {(ex['notional'] + notional) / eq:.1f}x exceeds the {gx}x cap: the executor will stop adding at the cap.")
    per_t: Dict[str, int] = {}
    for s in sigs:
        per_t[s["ticker"]] = per_t.get(s["ticker"], 0) + 1
    for t, n in per_t.items():
        if per_ticker_open(t) + n > int(rk["max_per_ticker"]):
            warnings.append(f"{t}: would exceed {rk['max_per_ticker']} concurrent trades per ticker.")
    return {"count": len(sigs), "est_risk": risk, "est_risk_pct": risk / max(eq, 1) * 100, "est_notional": notional,
            "est_gross_x": notional / max(eq, 1), "free_slots": slots, "equity": eq, "warnings": warnings,
            "tickers": [s["ticker"] for s in sigs]}


def lock_batch(entry_date: str, by: str) -> Dict[str, Any]:
    if not clock.window_open(entry_date):
        raise PermissionError("Review is locked: the batch cannot be confirmed right now (cut-off passed or next batch not published yet)")
    pv = batch_preview(entry_date)
    if pv["count"] == 0:
        raise ValueError("No confirmed (Trade) signals to queue")
    bid = db.x("INSERT INTO batches(entry_date,created_by,created_at,n_signals,est_risk,est_notional) VALUES(?,?,?,?,?,?)",
               (entry_date, by, db.now_iso(), pv["count"], pv["est_risk"], pv["est_notional"]))
    db.x(f"UPDATE signals SET exec_status='queued', batch_id=? WHERE entry_date=? AND {tradable_sql()} AND decision='trade' AND exec_status='none'",
         (bid, entry_date))
    db.audit(by, "batch_confirm", {"batch_id": bid, **pv})
    return {"batch_id": bid, **pv}


def unlock_batch(entry_date: str, by: str) -> int:
    if not clock.window_open(entry_date):
        raise PermissionError("Review is locked: the batch is final")
    n = db.q("SELECT COUNT(*) n FROM signals WHERE entry_date=? AND exec_status='queued'", (entry_date,))[0]["n"]
    db.x("UPDATE signals SET exec_status='none', batch_id=NULL WHERE entry_date=? AND exec_status='queued'", (entry_date,))
    db.x("UPDATE batches SET status='cancelled' WHERE entry_date=? AND status='queued'", (entry_date,))
    db.audit(by, "batch_unlock", {"entry_date": entry_date, "n": n})
    return n


def set_risk(sig_id: int, risk_pct: float, by: str) -> Dict[str, Any]:
    """Manual per-trade risk override (fraction of equity, e.g. 0.01 = 1%). Re-sizes the preview; the executor honours it."""
    sig = db.one("SELECT * FROM signals WHERE id=?", (sig_id,))
    if not sig:
        raise KeyError("signal not found")
    why = decision_allowed(sig)
    if why:
        raise PermissionError(why)
    cap = float(settings.cfg()["risk"].get("max_risk_pct_override", 0.02))
    if not (0.0005 <= risk_pct <= cap):
        raise ValueError(f"risk must be between 0.05% and {cap * 100:.2f}% of equity")
    sp = engine.size_preview(sig["ref_entry"] or sig["close"], sig["atr"], equity(), sig["risk_mult"] or 1.0, risk_pct=risk_pct)
    db.x("UPDATE signals SET risk_pct=?, risk_set_at=?, est_shares=?, est_risk=?, est_notional=? WHERE id=?",
         (risk_pct, db.now_iso(), sp["est_shares"], sp["est_risk"], sp["est_notional"], sig_id))
    db.audit(by, "risk_override", {"ticker": sig["ticker"], "risk_pct": risk_pct, "default": sig.get("risk_pct_default")})
    return db.one("SELECT * FROM signals WHERE id=?", (sig_id,))


def set_watch(sig_id: int, by: str) -> Dict[str, Any]:
    """Star an info-only setup: makes it tradable (manual risk default) and keeps it on the Watchlist."""
    sig = db.one("SELECT * FROM signals WHERE id=?", (sig_id,))
    if not sig:
        raise KeyError("signal not found")
    if sig["exec_status"] != "none":
        raise PermissionError(f"Signal already {sig['exec_status']}")
    sess = clock.status()["session_date"]
    mult = float(settings.cfg()["strategy"].get("manual_risk_mult", 0.5))
    if not sig.get("watch") and sig["tier"] not in tradable_tiers():
        rp = float(settings.cfg()["risk"]["risk_pct"]) * mult
        sp = engine.size_preview(sig["ref_entry"] or sig["close"], sig["atr"], equity(), mult, risk_pct=rp)
        db.x("UPDATE signals SET risk_mult=?, risk_pct=?, risk_pct_default=?, est_shares=?, est_risk=?, est_notional=? WHERE id=?",
             (mult, rp, rp, sp["est_shares"], sp["est_risk"], sp["est_notional"], sig_id))
    db.x("UPDATE signals SET watch=1, entry_date=? WHERE id=?", (sess, sig_id))
    db.audit(by, "watch", {"ticker": sig["ticker"]})
    return db.one("SELECT * FROM signals WHERE id=?", (sig_id,))


def roll_forward() -> int:
    """Session rollover. Unexecuted tradable/starred setups (and the latest scan batch) move to the new session with their last
    decision remembered (Watchlist); everything else, plus anything queued but never executed, expires (kept in the DB)."""
    c = settings.cfg()
    st = clock.status()
    sess, today_et = st["session_date"], str(clock.now_utc().astimezone(clock.ET).date())
    board = (db.kv_get("last_scan") or {}).get("signal_date") or "0000-00-00"
    oldest = str((clock.now_utc() - __import__("datetime").timedelta(days=int(c.get("watchlist", {}).get("max_age_days", 45)))).date())
    rolled = db.x(f"UPDATE signals SET last_decision=decision, last_decision_date=entry_date, decision='none', decided_by=NULL, decided_at=?, "
                  f"entry_date=? WHERE entry_date<? AND exec_status='none' AND ((signal_date>=? AND {tradable_sql()}) OR signal_date=?)",
                  (db.now_iso(), sess, sess, oldest, board))
    n = db.q("SELECT COUNT(*) n FROM signals WHERE entry_date<? AND exec_status IN ('none','queued')", (today_et,))[0]["n"]
    if n:
        db.x("UPDATE signals SET exec_status='expired' WHERE entry_date<? AND exec_status IN ('none','queued')", (today_et,))
    return n


expire_stale = roll_forward                                    # old name used by api/executor
