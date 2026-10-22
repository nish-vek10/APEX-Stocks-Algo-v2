# apex_desk/desk/sync.py
"""
Work PC <-> Supabase relay. Local SQLite stays the trading source of truth; Supabase serves the Netlify dashboard.

push:  session signals (static fields), charts (A/B/W), status, positions, audit            [service key]
pull:  web decisions (last-writer-wins by decided_at) and batch queue/unlock (cloud authoritative while none/queued);
       local execution results (executing/executed/failed/expired) are pushed back.
cmds:  'scan' requests from the web UI.
Env (apex_desk/.env):  DESK_SUPABASE_URL, DESK_SUPABASE_SERVICE_KEY   (service key = secret, Work PC only)
CLI:   python -m desk sync [--loop]
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from desk import clock, db, engine, rules, settings

log = logging.getLogger("desk.sync")
STATIC = ["signal_date", "entry_date", "ticker", "tier", "setup", "setup_tags", "stage", "stage_name", "prev_stage", "close", "atr", "atr_pct",
          "ema10", "ema20", "ema50", "ema200", "dist200", "days_since_s2", "donchian_upper", "bb_lower", "bb_mid", "volume_ratio", "rsi",
          "avg_dollar_vol", "ref_entry", "stop_price", "stop_dist", "stop_pct", "est_shares", "est_risk", "est_notional", "risk_mult"]
_scan_thread: Optional[threading.Thread] = None


def enabled() -> bool:
    return bool(settings.cfg().get("sync", {}).get("enabled")) and bool(os.getenv("DESK_SUPABASE_URL")) and bool(os.getenv("DESK_SUPABASE_SERVICE_KEY"))


class Cloud:
    def __init__(self) -> None:
        self.base = os.environ["DESK_SUPABASE_URL"].rstrip("/") + "/rest/v1"
        k = os.environ["DESK_SUPABASE_SERVICE_KEY"]
        self.h = {"apikey": k, "Authorization": f"Bearer {k}", "Content-Type": "application/json"}

    def _chk(self, r: requests.Response) -> requests.Response:
        if not r.ok:
            raise RuntimeError(f"Supabase {r.request.method} {r.url.split('?')[0].split('/')[-1]} -> {r.status_code}: {r.text[:300]}")
        return r

    def get(self, table: str, **params: str) -> List[Dict[str, Any]]:
        return self._chk(requests.get(f"{self.base}/{table}", headers=self.h, params=params, timeout=30)).json()

    def upsert(self, table: str, rows: List[Dict[str, Any]], conflict: str, chunk: int = 200) -> None:
        for i in range(0, len(rows), chunk):
            h = {**self.h, "Prefer": "resolution=merge-duplicates,return=minimal"}
            self._chk(requests.post(f"{self.base}/{table}", headers=h, params={"on_conflict": conflict}, json=rows[i:i + chunk], timeout=60))

    def patch(self, table: str, flt: Dict[str, str], body: Dict[str, Any]) -> None:
        self._chk(requests.patch(f"{self.base}/{table}", headers={**self.h, "Prefer": "return=minimal"}, params=flt, json=body, timeout=30))

    def delete(self, table: str, flt: Dict[str, str]) -> None:
        self._chk(requests.delete(f"{self.base}/{table}", headers=self.h, params=flt, timeout=30))


def _ts(v: Optional[str]) -> datetime:
    if not v:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def sid_of(r: Dict[str, Any]) -> str:
    return f"{r['signal_date']}|{r['ticker']}"


def _push_signals(c: Cloud, sess: str) -> List[Dict[str, Any]]:
    rows = db.q("SELECT * FROM signals WHERE entry_date=?", (sess,))
    cut = clock.cutoff_dt(sess).astimezone(timezone.utc).isoformat()
    out = [{"sid": sid_of(r), **{k: r[k] for k in STATIC}, "cutoff_at": cut} for r in rows]
    c.upsert("desk_signals", out, "sid")
    return rows


def _push_charts(c: Cloud, rows: List[Dict[str, Any]]) -> int:
    have = {r["sid"] for r in c.get("desk_charts", select="sid", limit="5000")}
    n = 0
    for r in rows:
        sid = sid_of(r)
        if r["tier"] not in rules.tradable_tiers() or sid in have:
            continue
        try:
            c.upsert("desk_charts", [{"sid": sid, "payload": engine.chart_payload(r["ticker"], r["signal_date"])}], "sid")
            n += 1
        except FileNotFoundError:
            continue
    return n


def _reconcile(c: Cloud, sess: str) -> Dict[str, int]:
    cloud = {r["sid"]: r for r in c.get("desk_signals", entry_date=f"eq.{sess}", limit="5000")}
    st = {"pulled": 0, "pushed": 0, "queued": 0, "unqueued": 0}
    for L in db.q("SELECT * FROM signals WHERE entry_date=?", (sess,)):
        C = cloud.get(sid_of(L))
        if not C or L["tier"] not in rules.tradable_tiers():
            continue
        # 1) local execution outcomes are authoritative
        if L["exec_status"] in ("executing", "executed", "failed", "expired"):
            if C["exec_status"] != L["exec_status"] or (C.get("exec_detail") or "") != (L["exec_detail"] or ""):
                c.patch("desk_signals", {"sid": f"eq.{C['sid']}"}, {"exec_status": L["exec_status"], "exec_detail": L["exec_detail"]})
                st["pushed"] += 1
            continue
        # 2) decision: last writer wins
        lc, ll = _ts(C.get("decided_at")), _ts(L["decided_at"])
        if lc > ll and clock.window_open(sess):
            db.x("UPDATE signals SET decision=?, decided_by=?, decided_at=?, note=? WHERE id=?",
                 (C["decision"], C.get("decided_by"), C["decided_at"], C.get("note"), L["id"]))
            db.x("INSERT INTO decisions(signal_id,decided_by,decision,note,decided_at) VALUES(?,?,?,?,?)",
                 (L["id"], C.get("decided_by") or "web", C["decision"], C.get("note"), C["decided_at"]))
            db.audit(C.get("decided_by") or "web", f"web_decision:{C['decision']}", {"ticker": L["ticker"], "via": "supabase"})
            st["pulled"] += 1
        elif ll > lc:
            c.patch("desk_signals", {"sid": f"eq.{C['sid']}"}, {"decision": L["decision"], "decided_by": L["decided_by"], "decided_at": L["decided_at"], "note": L["note"]})
            st["pushed"] += 1
        # 3) batch queue state: cloud authoritative while none/queued
        if C["exec_status"] == "queued" and L["exec_status"] == "none":
            cur = db.one("SELECT decision FROM signals WHERE id=?", (L["id"],))
            if cur and cur["decision"] == "trade":
                db.x("UPDATE signals SET exec_status='queued', batch_id=? WHERE id=?", (C.get("batch_id"), L["id"]))
                st["queued"] += 1
        elif C["exec_status"] == "none" and L["exec_status"] == "queued" and clock.window_open(sess):
            db.x("UPDATE signals SET exec_status='none', batch_id=NULL WHERE id=?", (L["id"],))
            st["unqueued"] += 1
    return st


def _push_status(c: Cloud) -> None:
    from desk import api
    p = api.status()
    p.pop("batch", None)
    p["synced_at"] = db.now_iso()
    c.upsert("desk_status", [{"id": 1, "payload": p, "updated_at": p["synced_at"]}], "id")


def _push_positions_audit(c: Cloud) -> None:
    pos = db.q("SELECT id,ticker,broker_symbol,ticket,entry_date,entry_price,stop_price,shares,mt5_volume,stop_distance,status,exit_date,exit_price,exit_reason,pnl_total,pnl_r FROM positions")
    if pos:
        c.upsert("desk_positions", pos, "id")
    aud = db.q("SELECT id,ts,actor,action,detail FROM audit ORDER BY id DESC LIMIT 200")
    if aud:
        c.upsert("desk_audit", aud, "id")


def _commands(c: Cloud) -> None:
    global _scan_thread
    for cmd in c.get("desk_commands", done_at="is.null", cmd="eq.scan"):
        if _scan_thread is None or not _scan_thread.is_alive():
            def work() -> None:
                engine.run_scan()
                cycle()
            _scan_thread = threading.Thread(target=work, daemon=True)
            _scan_thread.start()
        c.patch("desk_commands", {"id": f"eq.{cmd['id']}"}, {"done_at": db.now_iso()})


def _prune(c: Cloud) -> None:
    c.delete("desk_charts", {"created_at": f"lt.{(datetime.now(timezone.utc) - timedelta(days=10)).isoformat()}"})
    c.delete("desk_signals", {"entry_date": f"lt.{(datetime.now(timezone.utc) - timedelta(days=60)).date()}"})


def cycle(prune: bool = False) -> Dict[str, Any]:
    if not enabled():
        return {"enabled": False}
    c, sess = Cloud(), clock.status()["session_date"]
    rows = _push_signals(c, sess)
    charts = _push_charts(c, rows)
    rec = _reconcile(c, sess)
    _push_status(c)
    _push_positions_audit(c)
    _commands(c)
    if prune:
        _prune(c)
    return {"session": sess, "signals": len(rows), "charts_new": charts, **rec}


def loop() -> None:
    every = int(settings.cfg().get("sync", {}).get("interval_sec", 60))
    n = 0
    while True:
        try:
            print(time.strftime("%H:%M:%S"), cycle(prune=(n % 360 == 0)), flush=True)
        except Exception as e:  # noqa: BLE001 - keep relaying through transient network errors
            log.warning("sync error: %s", e)
            print("sync error:", e, flush=True)
        n += 1
        time.sleep(every)
