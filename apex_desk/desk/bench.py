# apex_desk/desk/bench.py
"""
Benchmark: do the stocks we pick beat the S&P 500?  PRICE ONLY: no position size, no P&L.
Day % = close / previous close - 1 (the overnight gap is included), for the S&P 500 and for every stock we hold that day.
  spx_pct   S&P 500 (OANDA SPX500_USD, 5-minute candles; the cash close = the 15:55 ET candle close)
  ew_pct    equal weight: simple average of the held stocks' day %  (1/N each)
  w_pct     risk weighted: weights follow the tier risk multiple (A 1.0, B / W 0.5, starred C 0.5), normalised to 100%
  port_pct  account equity now vs equity at the previous close (our real portfolio, includes sizing and costs)
A stock is held from its entry date to its exit date (a ticker entered twice counts once, at its highest multiple).
Completed days are rebuilt from the EOD cache + OANDA closes (rebuild(), 17:10 ET job + scheduler start = backfill).
Today is "live": live() runs every 5 minutes in market hours (MT5 prices for the held stocks, OANDA for the index, vs the previous close).
Env (.env, Work PC only):  DESK_OANDA_TOKEN  DESK_OANDA_ENV=practice|live   (optional DESK_OANDA_INSTRUMENT, default SPX500_USD)
CLI: python -m desk bench [--live]
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from desk import clock, db, engine, settings

log = logging.getLogger("desk.bench")

DDL = """
CREATE TABLE IF NOT EXISTS bench_daily (
  date TEXT PRIMARY KEY, spx_close REAL, spx_prev REAL, spx_pct REAL, ew_pct REAL, w_pct REAL, n_stocks INTEGER,
  equity REAL, equity_prev REAL, port_pct REAL, live INTEGER DEFAULT 0, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS bench_stock (
  date TEXT NOT NULL, ticker TEXT NOT NULL, prev_close REAL, close REAL, pct REAL, weight REAL, live INTEGER DEFAULT 0,
  PRIMARY KEY (date, ticker)
);
"""
_ready = False
CLOSE_START_ET = (15, 55)                                       # 5-minute candle that ends at the 16:00 ET cash close


def _ensure() -> None:
    global _ready
    if not _ready:
        db.conn().executescript(DDL)
        _ready = True


def configured() -> bool:
    return bool(os.getenv("DESK_OANDA_TOKEN"))


# ── OANDA (index) ──────────────────────────────────────────────────────────────
def _oanda(path: str, **params: Any) -> Dict[str, Any]:
    host = "api-fxtrade.oanda.com" if os.getenv("DESK_OANDA_ENV", "practice").lower() == "live" else "api-fxpractice.oanda.com"
    r = requests.get(f"https://{host}{path}", headers={"Authorization": f"Bearer {os.environ['DESK_OANDA_TOKEN']}"}, params=params, timeout=30)
    if not r.ok:
        raise RuntimeError(f"OANDA {r.status_code}: {r.text[:200]}")
    return r.json()


def _instrument() -> str:
    return os.getenv("DESK_OANDA_INSTRUMENT", "SPX500_USD")


def spx_closes(start: str) -> Dict[str, float]:
    """{ET date: S&P 500 cash close} from 5-minute candles since `start` (cached; only missing days are fetched)."""
    have: Dict[str, float] = db.kv_get("spx_close") or {}
    if not configured():
        return have
    today = str(clock.now_utc().astimezone(clock.ET).date())
    need_from = max(start, min([d for d in pd.bdate_range(start, today).strftime("%Y-%m-%d") if d not in have and d < today] or [today]))
    if need_from >= today:
        return have
    cur = datetime.fromisoformat(need_from).replace(tzinfo=clock.ET).astimezone(timezone.utc) - timedelta(hours=6)
    end = clock.now_utc()
    found: Dict[str, float] = {}
    for _ in range(60):                                          # 5000 candles per page ~ 17 days
        j = _oanda(f"/v3/instruments/{_instrument()}/candles", granularity="M5", price="M", count=5000, **{"from": cur.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")})
        cs = [c for c in j.get("candles", []) if c.get("mid")]
        if not cs:
            break
        for c in cs:
            t = pd.Timestamp(c["time"]).tz_convert(clock.ET)
            if (t.hour, t.minute) == CLOSE_START_ET:
                found[str(t.date())] = float(c["mid"]["c"])
        last = pd.Timestamp(cs[-1]["time"]).to_pydatetime()
        if last >= end - timedelta(minutes=10) or len(cs) < 5000:
            break
        cur = last + timedelta(minutes=5)
    have.update({d: v for d, v in found.items() if d < today})
    db.kv_set("spx_close", have)
    return have


def spx_now() -> Optional[float]:
    if not configured():
        return None
    j = _oanda(f"/v3/instruments/{_instrument()}/candles", granularity="M5", price="M", count=2)
    cs = [c for c in j.get("candles", []) if c.get("mid")]
    return float(cs[-1]["mid"]["c"]) if cs else None


# ── helpers ────────────────────────────────────────────────────────────────────
def _mult(tier: Optional[str]) -> float:
    st = settings.cfg().get("strategy", {})
    t = (st.get("tiers") or {}).get(tier or "") or {}
    return float(t.get("risk_mult", st.get("manual_risk_mult", 0.5)))


def _holdings() -> List[Dict[str, Any]]:
    return db.q("SELECT ticker,tier,entry_date,exit_date,exit_price,status,broker_symbol FROM positions WHERE entry_date IS NOT NULL")


def _eq_close() -> Dict[str, float]:
    return db.kv_get("eq_close") or {}


def _agg(date: str) -> Dict[str, Any]:
    rows = db.q("SELECT pct,weight FROM bench_stock WHERE date=? AND pct IS NOT NULL", (date,))
    if not rows:
        return {"ew_pct": None, "w_pct": None, "n_stocks": 0}
    wsum = sum(r["weight"] for r in rows) or 1.0
    return {"ew_pct": sum(r["pct"] for r in rows) / len(rows), "w_pct": sum(r["pct"] * r["weight"] for r in rows) / wsum, "n_stocks": len(rows)}


def _write_day(date: str, spx: Dict[str, float], live: int, spx_close: Optional[float] = None) -> None:
    days = sorted(spx)
    prev_d = max([d for d in days if d < date], default=None)
    sc = spx_close if spx_close is not None else spx.get(date)
    sp = spx.get(prev_d) if prev_d else None
    eqs = _eq_close()
    eq = eqs.get(date)
    prev_e = max([d for d in eqs if d < date], default=None)
    eqp = eqs.get(prev_e) if prev_e else None
    a = _agg(date)
    db.x("INSERT INTO bench_daily(date,spx_close,spx_prev,spx_pct,ew_pct,w_pct,n_stocks,equity,equity_prev,port_pct,live,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
         "ON CONFLICT(date) DO UPDATE SET spx_close=excluded.spx_close, spx_prev=excluded.spx_prev, spx_pct=excluded.spx_pct, ew_pct=excluded.ew_pct, w_pct=excluded.w_pct, "
         "n_stocks=excluded.n_stocks, equity=excluded.equity, equity_prev=excluded.equity_prev, port_pct=excluded.port_pct, live=excluded.live, updated_at=excluded.updated_at",
         (date, sc, sp, (sc / sp - 1) if sc and sp else None, a["ew_pct"], a["w_pct"], a["n_stocks"], eq, eqp, (eq / eqp - 1) if eq and eqp else None, live, db.now_iso()))


# ── completed days ─────────────────────────────────────────────────────────────
def rebuild() -> Dict[str, Any]:
    """Backfill / refresh every completed day from the EOD cache (stocks) and OANDA 15:55 ET closes (index). Idempotent."""
    _ensure()
    pos = _holdings()
    if not pos:
        return {"positions": 0, "days": 0}
    start = min(p["entry_date"] for p in pos)
    try:
        spx = spx_closes((pd.Timestamp(start) - pd.Timedelta(days=7)).strftime("%Y-%m-%d"))
    except Exception as e:  # noqa: BLE001 - index leg is optional; stocks still build
        log.warning("OANDA index history failed: %s", e)
        spx = db.kv_get("spx_close") or {}
    bars: Dict[str, pd.DataFrame] = {}
    for t in {p["ticker"] for p in pos}:
        df = engine._read(t)
        if df is not None:
            bars[t] = df.assign(d=df["date"].dt.strftime("%Y-%m-%d")).set_index("d")
    cal = sorted({d for df in bars.values() for d in df.index if d >= start})
    if not cal:
        return {"positions": len(pos), "days": 0, "note": "no cached prices"}
    last = cal[-1]
    with db._LOCK:
        cx = db.conn()
        cx.execute("DELETE FROM bench_stock WHERE live=0 OR date<=?", (last,))
        for d in cal:
            held: Dict[str, Dict[str, Any]] = {}
            for p in pos:
                end = p["exit_date"] if p["status"] == "closed" and p["exit_date"] else "9999-12-31"
                if p["entry_date"] <= d <= end:
                    h = held.setdefault(p["ticker"], {"w": 0.0, "exit": None})
                    h["w"] = max(h["w"], _mult(p["tier"]))
                    if p["status"] == "closed" and p["exit_date"] == d and p["exit_price"]:
                        h["exit"] = p["exit_price"]
            for t, h in held.items():
                df = bars.get(t)
                if df is None or d not in df.index:
                    continue
                i = df.index.get_loc(d)
                if i == 0:
                    continue
                prev, px = float(df["close"].iloc[i - 1]), float(h["exit"] or df["close"].iloc[i])
                cx.execute("INSERT OR REPLACE INTO bench_stock(date,ticker,prev_close,close,pct,weight,live) VALUES(?,?,?,?,?,?,0)", (d, t, prev, px, px / prev - 1, h["w"]))
        cx.commit()
    for d in cal:
        _write_day(d, spx, 0)
    db.kv_set("bench_rev", db.now_iso())
    n = db.one("SELECT COUNT(*) n FROM bench_daily")["n"]
    log.info("Benchmark rebuilt: %d days, index %s", n, "OANDA" if configured() else "NOT configured (no DESK_OANDA_TOKEN)")
    return {"days": n, "latest": last, "index": configured()}


# ── today, every 5 minutes ─────────────────────────────────────────────────────
def live() -> Dict[str, Any]:
    """Running day % vs the previous close: held stocks (MT5 prices), S&P 500 (OANDA), account equity (MT5). Needs the scheduler's MT5 lock."""
    _ensure()
    from desk.executor import MT5_LOCK, Executor
    import core.utils.trading_calendar as tc
    now = clock.now_utc().astimezone(clock.ET)
    today = str(now.date())
    mins = now.hour * 60 + now.minute
    if not tc.is_trading_day(today) or not (9 * 60 + 30 <= mins <= 16 * 60 + 10):
        return {"skipped": "market closed"}
    pos = [p for p in _holdings() if p["status"] == "open"]
    if not pos or not MT5_LOCK.acquire(blocking=False):
        return {"skipped": "no positions or MT5 busy"}
    try:
        ex = Executor()
        ex.broker.connect()
        try:
            snap = ex.snapshot()
            held: Dict[str, Dict[str, Any]] = {}
            for p in pos:
                h = held.setdefault(p["ticker"], {"w": 0.0, "sym": p["broker_symbol"]})
                h["w"] = max(h["w"], _mult(p["tier"]))
            rows = []
            for t, h in held.items():
                df = engine._read(t)
                if df is None:
                    continue
                before = df[df["date"].dt.strftime("%Y-%m-%d") < today]
                tick = ex.broker.tick(h["sym"]) if h["sym"] else None
                if before.empty or tick is None:
                    continue
                prev, px = float(before["close"].iloc[-1]), (tick.bid + tick.ask) / 2
                rows.append((today, t, prev, px, px / prev - 1, h["w"]))
        finally:
            ex.broker.disconnect()
    finally:
        MT5_LOCK.release()
    with db._LOCK:
        cx = db.conn()
        cx.execute("DELETE FROM bench_stock WHERE date=?", (today,))
        cx.executemany("INSERT INTO bench_stock(date,ticker,prev_close,close,pct,weight,live) VALUES(?,?,?,?,?,?,1)", rows)
        cx.commit()
    eqs = _eq_close()
    if snap.get("equity"):
        eqs[today] = float(snap["equity"])                       # last reading up to 16:10 ET becomes the day's closing equity
        db.kv_set("eq_close", {d: eqs[d] for d in sorted(eqs)[-400:]})
    spx = db.kv_get("spx_close") or {}
    try:
        spx_close = spx_now()
    except Exception as e:  # noqa: BLE001
        log.warning("OANDA live index failed: %s", e)
        spx_close = None
    _write_day(today, spx, 1, spx_close)
    db.kv_set("bench_rev", db.now_iso())
    return {"date": today, "stocks": len(rows), "spx": spx_close}


# ── readers ────────────────────────────────────────────────────────────────────
def daily() -> List[Dict[str, Any]]:
    _ensure()
    return db.q("SELECT * FROM bench_daily ORDER BY date DESC")


def stocks() -> List[Dict[str, Any]]:
    _ensure()
    return db.q("SELECT * FROM bench_stock ORDER BY date DESC, ticker")
