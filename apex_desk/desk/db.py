# apex_desk/desk/db.py
"""
SQLite storage (single file, thread-safe). The schema mirrors docs/supabase_schema.sql so the same tables can
move to Supabase/Postgres later: only this module needs a Postgres adapter, the API/executor call these functions.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from desk import settings

_LOCK = threading.RLock()
_CONN: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_date TEXT NOT NULL, entry_date TEXT NOT NULL, ticker TEXT NOT NULL,
  tier TEXT NOT NULL,                       -- A core | B extended | W warming (all tradable) | C info only
  setup TEXT, setup_tags TEXT, risk_mult REAL,
  stage INTEGER, stage_name TEXT, prev_stage INTEGER,
  close REAL, atr REAL, atr_pct REAL,
  ema10 REAL, ema20 REAL, ema50 REAL, ema200 REAL,
  dist200 REAL, days_since_s2 INTEGER, donchian_upper REAL, bb_lower REAL, bb_mid REAL,
  volume_ratio REAL, rsi REAL, avg_dollar_vol REAL,
  ref_entry REAL, stop_price REAL, stop_dist REAL, stop_pct REAL,
  est_shares REAL, est_risk REAL, est_notional REAL,
  decision TEXT DEFAULT 'none',             -- none | trade | skip
  decided_by TEXT, decided_at TEXT, note TEXT,
  exec_status TEXT DEFAULT 'none',          -- none | queued | executing | executed | failed | expired
  exec_detail TEXT,
  batch_id INTEGER,
  created_at TEXT NOT NULL,
  UNIQUE(signal_date, ticker)
);
CREATE INDEX IF NOT EXISTS ix_sig_entry ON signals(entry_date, tier);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER NOT NULL, decided_by TEXT NOT NULL,
  decision TEXT NOT NULL, note TEXT, decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, entry_date TEXT NOT NULL, created_by TEXT, created_at TEXT NOT NULL,
  n_signals INTEGER, est_risk REAL, est_notional REAL, status TEXT DEFAULT 'queued'   -- queued | executed | cancelled
);
CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, ticker TEXT, ts TEXT NOT NULL,
  ok INTEGER, reason TEXT, retcode INTEGER, order_id INTEGER, lots REAL, shares REAL,
  fill_price REAL, stop_price REAL, risk_usd REAL, detail TEXT
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, broker_symbol TEXT, ticket INTEGER,
  signal_id INTEGER, entry_date TEXT, entry_price REAL, stop_price REAL, shares REAL, mt5_volume REAL,
  stop_distance REAL, status TEXT DEFAULT 'open',          -- open | closed
  exit_date TEXT, exit_price REAL, exit_reason TEXT, pnl_total REAL, pnl_r REAL
);
CREATE INDEX IF NOT EXISTS ix_pos_open ON positions(status, ticker);
CREATE TABLE IF NOT EXISTS pending_exits (
  ticker TEXT PRIMARY KEY, reason TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS kv ( k TEXT PRIMARY KEY, v TEXT );
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT, action TEXT, detail TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            _CONN = sqlite3.connect(str(settings.db_path()), check_same_thread=False, timeout=30)
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.executescript(SCHEMA)
            for col, typ in (("setup", "TEXT"), ("setup_tags", "TEXT"), ("risk_mult", "REAL"), ("sig_close", "REAL"), ("as_of", "TEXT"),
                             ("live_setup", "TEXT"), ("risk_pct", "REAL"), ("risk_pct_default", "REAL"), ("risk_set_at", "TEXT"),
                             ("watch", "INTEGER DEFAULT 0"), ("last_decision", "TEXT"), ("last_decision_date", "TEXT"),
                             ("broker_mode", "TEXT")):   # migrations for older DB files
                try:
                    _CONN.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass
            try:
                _CONN.execute("ALTER TABLE positions ADD COLUMN tier TEXT")      # tier tag (A/B/W/C) copied from the signal at entry
            except sqlite3.OperationalError:
                pass
            _CONN.execute("UPDATE positions SET tier=(SELECT tier FROM signals WHERE signals.id=positions.signal_id) WHERE tier IS NULL")
            _CONN.commit()
        return _CONN


def q(sql: str, args: Iterable = ()) -> List[Dict[str, Any]]:
    with _LOCK:
        cur = conn().execute(sql, tuple(args))
        return [dict(r) for r in cur.fetchall()]


def one(sql: str, args: Iterable = ()) -> Optional[Dict[str, Any]]:
    r = q(sql, args)
    return r[0] if r else None


def x(sql: str, args: Iterable = ()) -> int:
    with _LOCK:
        cur = conn().execute(sql, tuple(args))
        conn().commit()
        return cur.lastrowid


def kv_set(k: str, v: Any) -> None:
    x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v, default=str)))


def kv_get(k: str, default: Any = None) -> Any:
    r = one("SELECT v FROM kv WHERE k=?", (k,))
    return json.loads(r["v"]) if r else default


def audit(actor: str, action: str, detail: Any = None) -> None:
    x("INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)", (now_iso(), actor, action, json.dumps(detail, default=str)))


def upsert_signal(s: Dict[str, Any]) -> int:
    """Insert a new signal. Existing (signal_date,ticker) rows keep their decision/execution state; only market fields refresh."""
    cols = [k for k in s.keys()]
    existing = one("SELECT id, exec_status, entry_date FROM signals WHERE signal_date=? AND ticker=?", (s["signal_date"], s["ticker"]))
    if existing:
        if existing["exec_status"] == "expired" and existing["entry_date"] != s["entry_date"]:
            x("UPDATE signals SET exec_status='none', decision='none', decided_by=NULL, decided_at=NULL, note=NULL, batch_id=NULL WHERE id=?", (existing["id"],))   # test relabel only
            existing["exec_status"] = "none"
        if existing["exec_status"] in ("none",):
            skip = {"signal_date", "ticker", "sig_close"}
            if one("SELECT risk_set_at FROM signals WHERE id=?", (existing["id"],))["risk_set_at"]:
                skip |= {"risk_pct"}                              # keep a manual risk override
            sets = ",".join(f"{c}=?" for c in cols if c not in skip)
            vals = [s[c] for c in cols if c not in skip] + [existing["id"]]
            x(f"UPDATE signals SET {sets} WHERE id=?", vals)
        return existing["id"]
    cols = cols + ["created_at"]
    vals = [s[c] for c in s.keys()] + [now_iso()]
    return x(f"INSERT INTO signals({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
