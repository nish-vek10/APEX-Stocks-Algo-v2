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
          "avg_dollar_vol", "ref_entry", "stop_price", "stop_dist", "stop_pct", "est_shares", "est_risk", "est_notional", "risk_mult",
          "sig_close", "as_of", "live_setup", "risk_pct_default", "last_decision", "last_decision_date"]
KEEP_DAYS = 60                      # synced window for tradable / starred signals (the cloud keeps them much longer)
_last: Dict[str, int] = {}          # sid -> hash of the last pushed static payload (push only on change)
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


def _sync_rows(sess: str) -> List[Dict[str, Any]]:
    """Rows the cloud must mirror: current session + tradable/starred signals of the last KEEP_DAYS (incl. executed, for status)."""
    since = str((clock.now_utc() - timedelta(days=KEEP_DAYS)).date())
    return db.q(f"SELECT * FROM signals WHERE entry_date=? OR (signal_date>=? AND {rules.tradable_sql()})", (sess, since))


def _payload(r: Dict[str, Any]) -> Dict[str, Any]:
    cut = clock.cutoff_dt(r["entry_date"]).astimezone(timezone.utc).isoformat()
    return {"sid": sid_of(r), **{k: r[k] for k in STATIC}, "cutoff_at": cut}


def _insert_missing(c: Cloud, rows: List[Dict[str, Any]], have: Dict[str, Any]) -> None:
    new = []
    for r in rows:
        if sid_of(r) in have:
            continue
        p = _payload(r)
        p.update(risk_pct=r["risk_pct"], risk_set_at=r["risk_set_at"], watch=bool(r["watch"]))
        new.append(p)
    for i in range(0, len(new), 200):
        h = {**c.h, "Prefer": "resolution=ignore-duplicates,return=minimal"}
        c._chk(requests.post(f"{c.base}/desk_signals", headers=h, params={"on_conflict": "sid"}, json=new[i:i + 200], timeout=60))


def _push_changed(c: Cloud, rows: List[Dict[str, Any]]) -> int:
    out = []
    for r in rows:
        p = _payload(r)
        h = hash(str(sorted(p.items(), key=lambda kv: kv[0])))
        if _last.get(p["sid"]) != h:
            out.append((p, h))
    if out:
        c.upsert("desk_signals", [p for p, _ in out], "sid")
        for p, h in out:
            _last[p["sid"]] = h
    return len(out)


def _push_charts(c: Cloud, rows: List[Dict[str, Any]]) -> int:
    done = db.kv_get("chart_pushed") or {}
    have = {r["sid"] for r in c.get("desk_charts", select="sid", limit="10000")}
    n = 0
    for r in rows:
        sid = sid_of(r)
        if not rules.is_tradable(r) or r["exec_status"] != "none":
            continue
        if sid in have and done.get(sid) == r.get("as_of"):
            continue
        try:
            c.upsert("desk_charts", [{"sid": sid, "payload": engine.chart_payload(r["ticker"]), "created_at": db.now_iso()}], "sid")
            done[sid] = r.get("as_of")
            n += 1
        except FileNotFoundError:
            continue
    db.kv_set("chart_pushed", done)
    return n


def _reconcile(c: Cloud, rows: List[Dict[str, Any]]) -> Dict[str, int]:
    since = str((clock.now_utc() - timedelta(days=KEEP_DAYS)).date())
    cloud = {r["sid"]: r for r in c.get("desk_signals", signal_date=f"gte.{since}", limit="10000")}
    st = {"pulled": 0, "pushed": 0, "queued": 0, "unqueued": 0, "risk": 0, "watch": 0}
    for L in rows:
        C = cloud.get(sid_of(L))
        if not C or not rules.is_tradable(L) and not C.get("watch"):
            continue
        sid = f"eq.{C['sid']}"
        # 1) local execution outcomes are authoritative
        if L["exec_status"] in ("executing", "executed", "failed", "expired"):
            if C["exec_status"] != L["exec_status"] or (C.get("exec_detail") or "") != (L["exec_detail"] or ""):
                c.patch("desk_signals", {"sid": sid}, {"exec_status": L["exec_status"], "exec_detail": L["exec_detail"]})
                st["pushed"] += 1
            continue
        open_ = clock.window_open(L["entry_date"])
        # 2) star (watch): either side can set it, nobody clears it
        if C.get("watch") and not L["watch"]:
            rules.set_watch(L["id"], C.get("decided_by") or "web")
            st["watch"] += 1
            L = db.one("SELECT * FROM signals WHERE id=?", (L["id"],))
        elif L["watch"] and not C.get("watch"):
            c.patch("desk_signals", {"sid": sid}, {"watch": True, "risk_pct": L["risk_pct"], "risk_pct_default": L["risk_pct_default"]})
        # 3) decision: last writer wins
        lc, ll = _ts(C.get("decided_at")), _ts(L["decided_at"])
        if lc > ll and open_:
            db.x("UPDATE signals SET decision=?, decided_by=?, decided_at=?, note=? WHERE id=?",
                 (C["decision"], C.get("decided_by"), C["decided_at"], C.get("note"), L["id"]))
            db.x("INSERT INTO decisions(signal_id,decided_by,decision,note,decided_at) VALUES(?,?,?,?,?)",
                 (L["id"], C.get("decided_by") or "web", C["decision"], C.get("note"), C["decided_at"]))
            db.audit(C.get("decided_by") or "web", f"web_decision:{C['decision']}", {"ticker": L["ticker"], "via": "supabase"})
            st["pulled"] += 1
        elif ll > lc:
            c.patch("desk_signals", {"sid": sid}, {"decision": L["decision"], "decided_by": L["decided_by"], "decided_at": L["decided_at"], "note": L["note"]})
            st["pushed"] += 1
        # 4) manual risk override: last writer wins
        cr, lr = C.get("risk_pct"), L["risk_pct"]
        if cr is not None and lr is not None and abs(cr - lr) > 1e-9:
            if _ts(C.get("risk_set_at")) > _ts(L["risk_set_at"]) and open_:
                try:
                    rules.set_risk(L["id"], float(cr), C.get("decided_by") or "web")
                    db.x("UPDATE signals SET risk_set_at=? WHERE id=?", (C["risk_set_at"], L["id"]))
                    st["risk"] += 1
                except (ValueError, PermissionError) as e:
                    log.warning("risk pull refused %s: %s", L["ticker"], e)
            elif _ts(L["risk_set_at"]) > _ts(C.get("risk_set_at")):
                c.patch("desk_signals", {"sid": sid}, {"risk_pct": lr, "risk_set_at": L["risk_set_at"]})
        # 5) batch queue state: cloud authoritative while none/queued
        cur = db.one("SELECT decision, exec_status FROM signals WHERE id=?", (L["id"],))
        if C["exec_status"] == "queued" and cur["exec_status"] == "none" and cur["decision"] == "trade":
            db.x("UPDATE signals SET exec_status='queued', batch_id=? WHERE id=?", (C.get("batch_id"), L["id"]))
            st["queued"] += 1
        elif C["exec_status"] == "none" and cur["exec_status"] == "queued" and open_:
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
    c.delete("desk_charts", {"created_at": f"lt.{(datetime.now(timezone.utc) - timedelta(days=50)).isoformat()}"})
    c.delete("desk_signals", {"signal_date": f"lt.{(datetime.now(timezone.utc) - timedelta(days=400)).date()}"})


def cycle(prune: bool = False) -> Dict[str, Any]:
    if not enabled():
        return {"enabled": False}
    c, sess = Cloud(), clock.status()["session_date"]
    rules.roll_forward()
    rows = _sync_rows(sess)
    have = {r["sid"]: r for r in c.get("desk_signals", select="sid", signal_date=f"gte.{str((clock.now_utc() - timedelta(days=KEEP_DAYS)).date())}", limit="10000")}
    _insert_missing(c, rows, have)
    rec = _reconcile(c, rows)
    rows = _sync_rows(sess)                                    # re-read: reconcile may have changed local rows
    pushed = _push_changed(c, rows)
    charts = _push_charts(c, rows)
    _push_status(c)
    _push_positions_audit(c)
    _commands(c)
    if prune:
        _prune(c)
    return {"session": sess, "rows": len(rows), "changed": pushed, "charts_new": charts, **rec}


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
