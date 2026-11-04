# apex_desk/desk/pnl.py
"""
Daily P&L history. One close-of-business snapshot per day, built from the EOD price cache + the positions table.

pnl_pos_daily : one row per (position, trading day): that day's close and the position's running P&L at that close.
pnl_daily     : portfolio roll-up per day: combined P&L of every position open at that close, positions opened that day,
                combined risk of those, and total open risk.
Running P&L = (close - fill) * shares. Intraday, live_marks() (every minute) keeps today's row current at live prices (status 'live'). On a position's exit day the row holds the realised P&L (status 'exit') and stops there.
rebuild() is idempotent: the first call backfills every past day; later calls only (re)write the latest bar and any exit day,
earlier rows stay frozen (a later cache adjustment never rewrites history).
CLI: python -m desk pnl
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List

import pandas as pd

from desk import db, engine, settings

log = logging.getLogger("desk.pnl")

DDL = """
CREATE TABLE IF NOT EXISTS pnl_pos_daily (
  position_id INTEGER NOT NULL, date TEXT NOT NULL, ticker TEXT NOT NULL, close REAL, pnl REAL, status TEXT,
  PRIMARY KEY (position_id, date)
);
CREATE TABLE IF NOT EXISTS pnl_daily (
  date TEXT PRIMARY KEY, pnl REAL, n_open INTEGER, n_opened INTEGER, risk_opened REAL, risk_open REAL
);
"""
_ready = False


def _ensure() -> None:
    global _ready
    if not _ready:
        db.conn().executescript(DDL)
        for col, typ in (("realised", "REAL"), ("equity", "REAL"), ("live", "INTEGER DEFAULT 0")):          # migrations for older DB files
            try:
                db.conn().execute(f"ALTER TABLE pnl_daily ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        _ready = True


def start_equity() -> float:
    return float(settings.cfg().get("bench", {}).get("start_equity", 100000.0))


def _closes(ticker: str) -> Dict[str, float]:
    df = engine._read(ticker)
    if df is None:
        return {}
    return {d.strftime("%Y-%m-%d"): float(c) for d, c in zip(df["date"], df["close"])}


def _aggregate(cx: sqlite3.Connection) -> None:
    """Portfolio roll-up per day from the per-position rows, plus model equity = start equity + P&L since inception
    (open positions at the close + the realised result of every position already exited). Caller holds the lock and commits."""
    cx.execute("DELETE FROM pnl_daily")
    cx.execute("INSERT INTO pnl_daily(date,pnl,n_open,n_opened,risk_opened,risk_open,live) "
               "SELECT d.date, ROUND(SUM(d.pnl),2), COUNT(*), SUM(CASE WHEN p.entry_date=d.date THEN 1 ELSE 0 END), "
               "ROUND(SUM(CASE WHEN p.entry_date=d.date THEN p.shares*p.stop_distance ELSE 0 END),2), ROUND(SUM(p.shares*p.stop_distance),2), "
               "MAX(CASE WHEN d.status='live' THEN 1 ELSE 0 END) "
               "FROM pnl_pos_daily d JOIN positions p ON p.id=d.position_id GROUP BY d.date")
    done = sorted((r["exit_date"], r["pnl_total"] if r["pnl_total"] is not None else ((r["exit_price"] or r["entry_price"]) - r["entry_price"]) * r["shares"])
                  for r in cx.execute("SELECT exit_date,pnl_total,exit_price,entry_price,shares FROM positions "
                                      "WHERE status='closed' AND exit_date IS NOT NULL AND entry_price IS NOT NULL").fetchall())
    base = start_equity()
    for r in cx.execute("SELECT date, pnl FROM pnl_daily").fetchall():
        realised = round(sum(v for d, v in done if d < r["date"]), 2)             # exit-day P&L is already inside that day's pnl
        cx.execute("UPDATE pnl_daily SET realised=?, equity=? WHERE date=?", (realised, round(base + r["pnl"] + realised, 2), r["date"]))


def live_marks(prices: Dict[str, float], today: str) -> Dict[str, Any]:
    """Intraday snapshot (every minute in market hours): mark every open position at the given prices, add positions exited today at their
    realised result, and refresh today's roll-up. The 17:10 ET rebuild replaces these rows with the official closes."""
    _ensure()
    pos = db.q("SELECT id,ticker,entry_date,entry_price,shares,status,exit_date,exit_price,pnl_total FROM positions "
               "WHERE entry_price IS NOT NULL AND shares IS NOT NULL AND entry_date IS NOT NULL")
    last = {r["position_id"]: r["close"] for r in db.q("SELECT position_id,close FROM pnl_pos_daily WHERE date<? ORDER BY date", (today,))}
    rows, live_ids = [], []
    for p in pos:
        if p["status"] == "open" and p["entry_date"] <= today:
            px = prices.get(p["ticker"])
            if px is None:
                px = last.get(p["id"], p["entry_price"])                           # no quote this minute: carry the last known price
            rows.append((p["id"], today, p["ticker"], round(px, 6), round((px - p["entry_price"]) * p["shares"], 2), "live"))
            live_ids.append(p["id"])
        elif p["status"] == "closed" and p["exit_date"] == today:
            res = p["pnl_total"] if p["pnl_total"] is not None else ((p["exit_price"] or p["entry_price"]) - p["entry_price"]) * p["shares"]
            rows.append((p["id"], today, p["ticker"], round(p["exit_price"] or p["entry_price"], 6), round(res, 2), "exit"))
    with db._LOCK:
        cx = db.conn()
        cx.executemany("INSERT INTO pnl_pos_daily(position_id,date,ticker,close,pnl,status) VALUES(?,?,?,?,?,?) "
                       "ON CONFLICT(position_id,date) DO UPDATE SET ticker=excluded.ticker, close=excluded.close, pnl=excluded.pnl, status=excluded.status", rows)
        keep = [r[0] for r in rows]
        cx.execute(f"DELETE FROM pnl_pos_daily WHERE date=? AND position_id NOT IN ({','.join('?' * len(keep)) or 'NULL'})", [today, *keep])
        _aggregate(cx)
        cx.commit()
    db.kv_set("pnl_rev", db.now_iso())
    row = db.one("SELECT pnl, equity FROM pnl_daily WHERE date=?", (today,)) or {}
    return {"positions": len(live_ids), "pnl": row.get("pnl"), "equity": row.get("equity")}


def rebuild() -> Dict[str, Any]:
    """Backfill / refresh the daily P&L tables. Safe to run any number of times."""
    _ensure()
    pos = db.q("SELECT id,ticker,entry_date,entry_price,shares,stop_distance,status,exit_date,exit_price,pnl_total FROM positions "
               "WHERE entry_price IS NOT NULL AND shares IS NOT NULL AND entry_date IS NOT NULL")
    if not pos:
        return {"positions": 0, "rows": 0}
    closes = {t: _closes(t) for t in {p["ticker"] for p in pos}}
    cal = sorted({d for c in closes.values() for d in c})                      # trading days seen in the cache
    if not cal:
        return {"positions": len(pos), "rows": 0, "note": "no cached prices"}
    last = cal[-1]
    have = {(r["position_id"], r["date"]) for r in db.q("SELECT position_id,date FROM pnl_pos_daily")}
    rows: List[tuple] = []
    for p in pos:
        c = closes.get(p["ticker"]) or {}
        closed = p["status"] == "closed" and p["exit_date"]
        end = min(p["exit_date"], last) if closed else last
        prev = None
        for d in cal:
            if d < p["entry_date"]:
                continue
            if d > end:
                break
            px = c.get(d, prev)                                                # carry the last close over a missing bar
            if px is None:
                continue
            prev = px
            is_exit = bool(closed) and d == p["exit_date"]
            if is_exit:
                pnl = p["pnl_total"] if p["pnl_total"] is not None else ((p["exit_price"] if p["exit_price"] is not None else px) - p["entry_price"]) * p["shares"]
                px_show = p["exit_price"] if p["exit_price"] is not None else px
            else:
                pnl, px_show = (px - p["entry_price"]) * p["shares"], px
            if (p["id"], d) in have and d < last and not is_exit:
                continue                                                       # frozen history
            rows.append((p["id"], d, p["ticker"], round(px_show, 6), round(pnl, 2), "exit" if is_exit else "open"))
        if closed and p["exit_date"] not in c and p["exit_date"] > last:
            pass                                                               # exit day not in the cache yet: written after the next cache refresh
    with db._LOCK:
        cx = db.conn()
        cx.executemany("INSERT INTO pnl_pos_daily(position_id,date,ticker,close,pnl,status) VALUES(?,?,?,?,?,?) "
                       "ON CONFLICT(position_id,date) DO UPDATE SET ticker=excluded.ticker, close=excluded.close, pnl=excluded.pnl, status=excluded.status", rows)
        cx.execute("DELETE FROM pnl_pos_daily WHERE position_id IN (SELECT id FROM positions WHERE status='closed' AND exit_date IS NOT NULL) "
                   "AND date > (SELECT exit_date FROM positions WHERE positions.id=pnl_pos_daily.position_id)")      # nothing after a late-detected exit
        cx.execute("DELETE FROM pnl_pos_daily WHERE position_id NOT IN (SELECT id FROM positions)")
        _aggregate(cx)
        cx.commit()
    n = db.one("SELECT COUNT(*) AS n FROM pnl_daily")["n"]
    db.kv_set("pnl_rev", db.now_iso())
    log.info("PnL snapshot: %d position-days written, %d portfolio days", len(rows), n)
    return {"positions": len(pos), "rows": len(rows), "days": n, "latest": last}


def daily(since: str = "0000-00-00") -> List[Dict[str, Any]]:
    _ensure()
    return db.q("SELECT * FROM pnl_daily WHERE date>=? ORDER BY date DESC", (since,))


def per_position(since: str = "0000-00-00") -> List[Dict[str, Any]]:
    _ensure()
    return db.q("SELECT * FROM pnl_pos_daily WHERE date>=? ORDER BY date DESC, ticker", (since,))
