# apex_desk/desk/bench.py
"""
Benchmark: do the stocks we pick beat the S&P 500?  PRICE ONLY: no position size, no P&L.
Day % = close / previous TRADING-day close - 1 (the overnight gap is included), for the S&P 500 and for every stock held that day.
  spx_pct   S&P 500 (OANDA SPX500_USD, 5-minute candles; cash close = the close of the last candle that starts before 16:00 ET, i.e. 15:55 ET)
  ew_pct    equal weight: simple average of the held stocks' day %  (1/N each)
  w_pct     risk weighted: weights follow the tier risk multiple (A 1.0, B / W 0.5, starred C 0.5), normalised to 100%
  port_pct  model equity vs the previous close: start equity (bench.start_equity, 100,000) + all P&L since inception (open positions at the close + realised), from the Daily PnL snapshot
A stock is held from its entry date to its exit date (a ticker entered twice counts once, at its highest multiple).
Schedule: live() every minute in market hours (also writes the Live PnL row), rebuild() 17:10 ET + scheduler start (history / backfill). Needs pnl.rebuild() first (equity).
A missing close never produces a multi-day move: if the previous trading day has no close, the % stays blank.
Env (.env, Work PC only):  DESK_OANDA_TOKEN  DESK_OANDA_ENV=practice|live   (optional DESK_OANDA_INSTRUMENT, default SPX500_USD)
CLI: python -m desk bench [--live | --check | --refresh | --recon]
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from desk import clock, db, engine, settings  # noqa: E402  (settings puts the repo root on sys.path)
import core.utils.trading_calendar as tc  # noqa: E402

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
CASH_CLOSE_MIN = 16 * 60                                         # 16:00 ET
OPEN_MIN = 9 * 60 + 30


def _ensure() -> None:
    global _ready
    if not _ready:
        db.conn().executescript(DDL)
        _ready = True


def configured() -> bool:
    return bool(os.getenv("DESK_OANDA_TOKEN"))


def _et_now() -> pd.Timestamp:
    return pd.Timestamp(clock.now_utc()).tz_convert(clock.ET)


def _prev_td(d: str) -> Optional[str]:
    """Previous US trading day before date d (None if not found within 10 days)."""
    t = pd.Timestamp(d)
    for _ in range(10):
        t -= pd.Timedelta(days=1)
        if tc.is_trading_day(t):
            return str(t.date())
    return None


def _trading_days(a: str, b: str) -> List[str]:
    return [str(t.date()) for t in pd.bdate_range(a, b) if tc.is_trading_day(t)]


# ── OANDA (index) ──────────────────────────────────────────────────────────────
def _oanda(path: str, **params: Any) -> Dict[str, Any]:
    host = "api-fxtrade.oanda.com" if os.getenv("DESK_OANDA_ENV", "practice").lower() == "live" else "api-fxpractice.oanda.com"
    r = requests.get(f"https://{host}{path}", headers={"Authorization": f"Bearer {os.environ['DESK_OANDA_TOKEN']}"}, params=params, timeout=30)
    if not r.ok:
        raise RuntimeError(f"OANDA {r.status_code}: {r.text[:200]}")
    return r.json()


def _instrument() -> str:
    return os.getenv("DESK_OANDA_INSTRUMENT", "SPX500_USD")


def _utc_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


def spx_closes(start: str, refresh: bool = False) -> Dict[str, float]:
    """{ET date: S&P 500 cash close}. Cash close = close of the last 5-minute candle that STARTS before 16:00 ET (must be 15:50 or 15:55).
    Cached in kv; only missing trading days are fetched; refresh=True re-pulls everything."""
    have: Dict[str, float] = {} if refresh else (db.kv_get("spx_close") or {})
    if not configured():
        return have
    today = str(_et_now().date())
    missing = [d for d in _trading_days(start, today) if d < today and d not in have]
    if not missing:
        return have
    cur = datetime.fromisoformat(missing[0]).replace(tzinfo=clock.ET) - timedelta(hours=6)
    end = clock.now_utc()
    last_c: Dict[str, tuple] = {}
    for _ in range(80):                                          # 5000 candles per page ~ 17 days
        j = _oanda(f"/v3/instruments/{_instrument()}/candles", granularity="M5", price="M", count=5000, **{"from": _utc_str(cur)})
        cs = [c for c in j.get("candles", []) if c.get("mid")]
        if not cs:
            break
        for c in cs:
            t = pd.Timestamp(c["time"]).tz_convert(clock.ET)
            m = t.hour * 60 + t.minute
            if OPEN_MIN <= m < CASH_CLOSE_MIN:
                last_c[str(t.date())] = (m, float(c["mid"]["c"]))   # chronological: the last one before 16:00 wins
        last = pd.Timestamp(cs[-1]["time"]).to_pydatetime()
        if last >= end - timedelta(minutes=10) or len(cs) < 5000:
            break
        cur = last + timedelta(minutes=5)
    for d, (m, px) in last_c.items():
        if d < today and m >= CASH_CLOSE_MIN - 10:               # 15:50 / 15:55 candle; anything earlier = gap or early close: leave blank
            have[d] = px
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


def _equity_by_day() -> Dict[str, float]:
    """Model equity per close = start equity + P&L since inception (open positions at the close + realised), from the Daily PnL snapshot."""
    from desk import pnl
    return {r["date"]: r["equity"] for r in pnl.daily() if r["equity"] is not None}


def _agg(date: str) -> Dict[str, Any]:
    rows = db.q("SELECT pct,weight FROM bench_stock WHERE date=? AND pct IS NOT NULL", (date,))
    if not rows:
        return {"ew_pct": None, "w_pct": None, "n_stocks": 0}
    wsum = sum(r["weight"] for r in rows) or 1.0
    return {"ew_pct": sum(r["pct"] for r in rows) / len(rows), "w_pct": sum(r["pct"] * r["weight"] for r in rows) / wsum, "n_stocks": len(rows)}


def _write_day(date: str, spx: Dict[str, float], live: int, spx_close: Optional[float] = None, equity: Optional[float] = None,
               eqs: Optional[Dict[str, float]] = None) -> None:
    from desk import pnl
    prev_d = _prev_td(date)
    sc = spx_close if spx_close is not None else spx.get(date)
    sp = spx.get(prev_d) if prev_d else None                      # strictly the previous TRADING day, never an older one
    eqs = eqs if eqs is not None else _equity_by_day()
    eq = equity if equity is not None else eqs.get(date)
    eqp = eqs.get(prev_d) if prev_d else None
    if eqp is None and eqs and date <= min(eqs) and prev_d not in eqs:
        eqp = pnl.start_equity()                                   # first day: change vs the starting equity
    a = _agg(date)
    db.x("INSERT INTO bench_daily(date,spx_close,spx_prev,spx_pct,ew_pct,w_pct,n_stocks,equity,equity_prev,port_pct,live,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
         "ON CONFLICT(date) DO UPDATE SET spx_close=excluded.spx_close, spx_prev=excluded.spx_prev, spx_pct=excluded.spx_pct, ew_pct=excluded.ew_pct, w_pct=excluded.w_pct, "
         "n_stocks=excluded.n_stocks, equity=excluded.equity, equity_prev=excluded.equity_prev, port_pct=excluded.port_pct, live=excluded.live, updated_at=excluded.updated_at",
         (date, sc, sp, (sc / sp - 1) if sc and sp else None, a["ew_pct"], a["w_pct"], a["n_stocks"], eq, eqp, (eq / eqp - 1) if eq and eqp else None, live, db.now_iso()))


# ── completed days ─────────────────────────────────────────────────────────────
def rebuild(refresh_index: bool = False) -> Dict[str, Any]:
    """Backfill / refresh every completed day from the EOD cache (stocks) and OANDA closes (index). Idempotent."""
    _ensure()
    pos = _holdings()
    if not pos:
        return {"positions": 0, "days": 0}
    start = min(p["entry_date"] for p in pos)
    try:
        spx = spx_closes(_prev_td(start) or start, refresh_index)
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
    eqs = _equity_by_day()
    for d in cal:
        _write_day(d, spx, 0, eqs=eqs)
    db.kv_set("bench_rev", db.now_iso())
    db.kv_set("bench_full", None)                                           # history may have changed: push every row once on the next sync
    n = db.one("SELECT COUNT(*) n FROM bench_daily")["n"]
    log.info("Benchmark rebuilt: %d days, index %s", n, "OANDA" if configured() else "NOT configured (no DESK_OANDA_TOKEN)")
    return {"days": n, "latest": last, "index": configured()}


# ── today ──────────────────────────────────────────────────────────────────────
def live() -> Dict[str, Any]:
    """Every minute, 09:30-16:00 ET: running day % vs the previous close for the held stocks (MT5 mid; P&L marks use the bid), the S&P 500 (OANDA) and the model equity
    (start equity + realised + open positions marked at those prices). Needs the scheduler's MT5 lock."""
    _ensure()
    from desk import pnl
    from desk.executor import MT5_LOCK, Executor
    now = _et_now()
    today = str(now.date())
    mins = now.hour * 60 + now.minute
    if not tc.is_trading_day(today) or not (OPEN_MIN <= mins <= CASH_CLOSE_MIN):
        return {"skipped": "market closed"}
    pos = [p for p in _holdings() if p["status"] == "open"]
    if not pos or not MT5_LOCK.acquire(blocking=False):
        return {"skipped": "no positions or MT5 busy"}
    px_now: Dict[str, float] = {}
    rows: List[tuple] = []
    try:
        ex = Executor()
        ex.broker.connect()
        try:
            try:
                ex.snapshot()                                                  # MT5 account snapshot every minute: the Equity card (equity, balance, time)
            except Exception as e:  # noqa: BLE001
                log.warning("MT5 account snapshot failed: %s", e)
            held: Dict[str, Dict[str, Any]] = {}
            for p in pos:
                h = held.setdefault(p["ticker"], {"w": 0.0, "sym": p["broker_symbol"]})
                h["w"] = max(h["w"], _mult(p["tier"]))
            for t, h in held.items():
                df = engine._read(t)
                if df is None:
                    continue
                before = df[df["date"].dt.strftime("%Y-%m-%d") < today]
                tick = ex.broker.tick(h["sym"]) if h["sym"] else None
                if before.empty or tick is None:
                    continue
                prev, px = float(before["close"].iloc[-1]), (tick.bid + tick.ask) / 2
                px_now[t] = float(tick.bid)                                   # P&L is marked at the bid, like MT5 values long positions
                rows.append((today, t, prev, px, px / prev - 1, h["w"]))
        finally:
            ex.broker.disconnect()
    finally:
        MT5_LOCK.release()
    for p in _holdings():                                                    # stocks exited today (stop / Stage 9 at the open) stay in today's basket at their exit price
        if p["status"] == "closed" and p["exit_date"] == today and p["exit_price"] and p["ticker"] not in {r[1] for r in rows}:
            df = engine._read(p["ticker"])
            before = df[df["date"].dt.strftime("%Y-%m-%d") < today] if df is not None else None
            if before is not None and not before.empty:
                prev = float(before["close"].iloc[-1])
                rows.append((today, p["ticker"], prev, float(p["exit_price"]), float(p["exit_price"]) / prev - 1, _mult(p["tier"])))
    with db._LOCK:
        cx = db.conn()
        cx.execute("DELETE FROM bench_stock WHERE date=?", (today,))
        cx.executemany("INSERT INTO bench_stock(date,ticker,prev_close,close,pct,weight,live) VALUES(?,?,?,?,?,?,1)", rows)
        cx.commit()
    marks = pnl.live_marks(px_now, today)                                # live P&L + model equity for today (the Live PnL card, every minute)
    eq_live = marks.get("equity")
    spx = db.kv_get("spx_close") or {}
    try:
        spx_px = spx_now()
    except Exception as e:  # noqa: BLE001
        log.warning("OANDA live index failed: %s", e)
        spx_px = None
    _write_day(today, spx, 1, spx_px, eq_live)
    db.kv_set("bench_rev", db.now_iso())
    return {"date": today, "stocks": len(rows), "spx": spx_px, "pnl": marks.get("pnl"), "equity": eq_live}


# ── reconciliation: desk model P&L vs the MT5 account (find any gap) ───────────────────────────────────────────
def recon() -> None:
    """Prints, per open desk position, the model P&L ((mid - fill) x shares) next to MT5's own position profit, then the totals vs account
    equity. Differences usually come from: positions on the account that are not desk positions, spread (mid vs bid), swap, or a missing quote."""
    _ensure()
    import MetaTrader5 as mt5
    from desk import pnl
    from desk.executor import MT5_LOCK, Executor
    rows = db.q("SELECT id,ticker,ticket,shares,entry_price,broker_symbol FROM positions WHERE status='open' AND entry_price IS NOT NULL ORDER BY ticker")
    with MT5_LOCK:
        ex = Executor()
        ex.broker.connect()
        try:
            acc = mt5.account_info()
            mt = {p.ticket: p for p in (mt5.positions_get() or [])}
            print(f"{'Ticker':<7}{'Ticket':>12}{'Shares':>9}{'Entry':>10}{'MT5 open':>10}{'Mid now':>10}{'Model P&L':>12}{'MT5 profit':>12}{'MT5 swap':>10}{'Gap':>10}")
            tm = tp = 0.0
            for r in rows:
                t = ex.broker.tick(r["broker_symbol"]) if r["broker_symbol"] else None
                mid = (t.bid + t.ask) / 2 if t else None
                model = (mid - r["entry_price"]) * r["shares"] if mid else 0.0
                m = mt.get(r["ticket"])
                prof = m.profit if m else None
                tm += model
                tp += prof or 0.0
                gap = (model - prof) if prof is not None else None
                print(f"{r['ticker']:<7}{r['ticket'] or 0:>12}{r['shares']:>9,.0f}{r['entry_price']:>10,.4f}{(m.price_open if m else 0):>10,.4f}{(mid or 0):>10,.4f}{model:>12,.2f}"
                      f"{(prof if prof is not None else float('nan')):>12,.2f}{(m.swap if m else 0):>10,.2f}{(gap if gap is not None else float('nan')):>10,.2f}"
                      + ("   <-- no MT5 position with this ticket" if not m else "") + ("   <-- no quote" if mid is None else ""))
            extra = [p for tk, p in mt.items() if tk not in {r["ticket"] for r in rows}]
            for p in extra:
                print(f"MT5-only position (not a desk position): {p.symbol} ticket={p.ticket} volume={p.volume} profit={p.profit:,.2f}")
            closed = db.one("SELECT COALESCE(SUM(pnl_total),0) AS v FROM positions WHERE status='closed'")["v"]
            print(f"\nTotals: model running {tm:,.2f} | MT5 floating {tp:,.2f} | all MT5 positions floating {sum(p.profit for p in mt.values()):,.2f}")
            print(f"Account: balance {acc.balance:,.2f} equity {acc.equity:,.2f} (equity - balance = {acc.equity - acc.balance:,.2f}) | profit field {acc.profit:,.2f}")
            print(f"Desk realised (closed positions, pnl_total) {closed:,.2f} | model equity {pnl.start_equity() + closed + tm:,.2f} vs MT5 equity {acc.equity:,.2f} vs start {pnl.start_equity():,.0f}: "
                  f"MT5 total P&L {acc.equity - pnl.start_equity():,.2f}, model {closed + tm:,.2f}")
        finally:
            ex.broker.disconnect()


# ── verification: our stored closes vs OANDA daily candles (16:00 ET alignment) and SPY (TwelveData, independent reference) ──
def check(days: int = 14) -> List[Dict[str, Any]]:
    """Prints our stored S&P closes next to OANDA's own daily OHLC (dailyAlignment 16:00 ET) and SPY % from TwelveData. Flags differences."""
    _ensure()
    if not configured():
        print("DESK_OANDA_TOKEN is not set")
        return []
    def candles(align: int) -> Dict[str, Dict[str, Any]]:
        j = _oanda(f"/v3/instruments/{_instrument()}/candles", granularity="D", price="M", count=days + 6, dailyAlignment=align, alignmentTimezone="America/New_York")
        res: Dict[str, Dict[str, Any]] = {}
        for c in j.get("candles", []):
            if c.get("mid") and c.get("complete"):
                st = pd.Timestamp(c["time"]).tz_convert(clock.ET)                      # candle starts at the alignment hour the evening before
                d = str((st + pd.Timedelta(hours=12)).date())
                if tc.is_trading_day(d):
                    res[d] = {**{k: float(c["mid"][k]) for k in "ohlc"}, "start": st, "end": st + pd.Timedelta(days=1)}
        return res

    win = lambda o: f"{o['start']:%a %d-%m %H:%M} -> {o['end']:%a %d-%m %H:%M} ET ({o['start'].tz_convert('Europe/London'):%H:%M} -> {o['end'].tz_convert('Europe/London'):%H:%M} London)"   # noqa: E731
    ohlc, tv = candles(16), candles(17)
    ds = sorted(ohlc)[-days:]
    spx = db.kv_get("spx_close") or {}                                          # exactly what the dashboard uses
    spy: Dict[str, float] = {}
    spy_msg = ""
    try:
        from dotenv import load_dotenv
        load_dotenv(settings.REPO_ROOT / ".env")
        r = requests.get("https://api.twelvedata.com/time_series", params={"symbol": "SPY", "interval": "1day", "outputsize": days + 6, "apikey": os.environ.get("TWELVEDATA_API_KEY", "")}, timeout=30).json()
        spy = {v["datetime"][:10]: float(v["close"]) for v in r.get("values", [])}
        spy_msg = "" if spy else f"SPY reference unavailable: {r.get('message') or 'no data (TWELVEDATA_API_KEY not found?)'}"
    except Exception as e:  # noqa: BLE001 - reference only
        spy_msg = f"SPY reference unavailable: {e}"
    pct = lambda a, b: "" if not a or not b else f"{(a / b - 1) * 100:+.2f}%"          # noqa: E731
    out: List[Dict[str, Any]] = []
    print("\n[1] WHAT THE DASHBOARD USES: OANDA daily candle, day ends 16:00 ET (= US cash close). Compare with the stored close.")
    print(f"{'Date':<11}{'Stored close':>13}{'D open':>10}{'high':>10}{'low':>10}{'D close':>10}{'Stored %':>10}{'D %':>8}{'SPY %':>8}  {'Flag':<8}Candle window")
    for d in ds:
        p = _prev_td(d)
        s, sp, o = spx.get(d), spx.get(p) if p else None, ohlc[d]
        po = ohlc.get(p) if p else None
        flag = "MISSING" if s is None else ("DIFF" if abs(s - o["c"]) > max(2.0, o["c"] * 0.0005) else "ok")
        print(f"{d:<11}{(f'{s:,.2f}' if s else '-'):>13}{o['o']:>10,.2f}{o['h']:>10,.2f}{o['l']:>10,.2f}{o['c']:>10,.2f}{pct(s, sp):>10}{pct(o['c'], po['c']) if po else '':>8}{(pct(spy.get(d), spy.get(p)) if p else ''):>8}  {flag:<8}{win(o)}")
        out.append({"date": d, "stored": s, **{f"d_{k}": o[k] for k in "ohlc"}, "flag": flag})
    print("\n[2] TRADINGVIEW STYLE: OANDA daily candle, day ends 17:00 ET (FX convention). This is the candle on a TradingView OANDA:SPX500USD daily chart.")
    print(f"{'Date':<11}{'Open':>10}{'High':>10}{'Low':>10}{'Close':>10}{'Day %':>8}  Candle window")
    for d in ds:
        p = _prev_td(d)
        o, po = tv.get(d), tv.get(p) if p else None
        if o:
            print(f"{d:<11}{o['o']:>10,.2f}{o['h']:>10,.2f}{o['l']:>10,.2f}{o['c']:>10,.2f}{pct(o['c'], po['c']) if po else '':>8}  {win(o)}")
    print("\nWhy they differ: [1] cuts the day at 16:00 ET so the S&P % lines up with the US stocks; TradingView cuts at 17:00 ET, so its open and close are an hour later. High and low are almost the same.")
    if spy_msg:
        print(spy_msg)
    return out


# ── readers ────────────────────────────────────────────────────────────────────
def daily() -> List[Dict[str, Any]]:
    _ensure()
    return db.q("SELECT * FROM bench_daily ORDER BY date DESC")


def stocks() -> List[Dict[str, Any]]:
    _ensure()
    return db.q("SELECT * FROM bench_stock ORDER BY date DESC, ticker")
