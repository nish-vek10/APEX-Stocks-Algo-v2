# apex_desk/desk/engine.py
"""
Signal engine: scans the daily cache, finds fresh Stage 6/7 breakouts with Stage-2 history (identical logic to the
validated research pipeline = production stage code), tiers them, previews sizing, stores them, and serves chart payloads.
READ-ONLY on the cache and on the old system's files.
"""
from __future__ import annotations

import glob
from functools import partial
import logging
import os
import time
from multiprocessing import Pool
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import yaml

from desk import clock, db, settings

from core.features.technicals.pipeline import IndicatorConfig, apply_indicators  # noqa: E402
from core.stages.stage_classifier import classify_stages  # noqa: E402
from core.utils.config_loader import load_production_config  # noqa: E402
from core.utils.universe_state import resolve_universe  # noqa: E402
from prod.signals.signal_generator import STAGE_NAMES  # noqa: E402

log = logging.getLogger("desk.engine")
MIN_BARS = 260
_IND = yaml.safe_load((settings.REPO_ROOT / "config" / "indicators.yaml").read_text(encoding="utf-8"))
_STG = yaml.safe_load((settings.REPO_ROOT / "config" / "stages.yaml").read_text(encoding="utf-8"))
ICFG = IndicatorConfig.from_dict(_IND)


# ── helpers ───────────────────────────────────────────────────────────────────
def _atr14(df: pd.DataFrame, period: int) -> pd.Series:
    h, l, c = df.high.astype(float), df.low.astype(float), df.close.astype(float)
    p = c.shift(1)
    tr = pd.concat([h - l, (h - p).abs(), (l - p).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _read(ticker: str) -> Optional[pd.DataFrame]:
    p = settings.cache_dir() / f"{ticker}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    from prod.data.twelvedata_fetcher import _drop_partial_today_bar
    return _drop_partial_today_bar(df).dropna(subset=["close"]).reset_index(drop=True)


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Indicators + stages (production code) + point-in-time signal features."""
    X = apply_indicators(df.copy(), ICFG)
    X["stage"] = classify_stages(X, _STG)["stage"].values
    X.loc[: MIN_BARS - 2, "stage"] = 1                      # backtest 260-bar floor
    X["atr14"] = _atr14(X, int(settings.cfg()["strategy"]["atr_period"]))
    s2 = (X.stage == 2).astype(int)
    X["s2_before"] = s2.shift(1).fillna(0).cummax().astype(bool)
    X["prev_stage"] = X.stage.shift(1).fillna(-1).astype(int)
    X["signal"] = X.stage.isin([6, 7]) & ~X.prev_stage.isin([6, 7]) & X.s2_before
    idx = np.arange(len(X))
    last2 = pd.Series(np.where(X.stage == 2, idx, np.nan)).ffill().shift(1)
    X["days_since_s2"] = idx - last2.values
    X["dist200"] = X.close / X.ema_200 - 1
    # 'Warming up' pre-breakout setups (research step 11). Same definitions as the study; first day of a run for W1.
    minp = float(settings.cfg()["strategy"]["min_price"])
    c, e10, e20, e50, du = X.close, X.ema_10, X.ema_20, X.ema_50, X.get("donchian_upper_s1", X.close * np.nan)
    base = X.s2_before & (X.dist200 <= -0.15) & (X.days_since_s2 <= 90) & (c >= minp)
    notbrk = ~X.stage.isin([6, 7])
    w1 = base & (e10 > e20) & (c >= 0.97 * du) & (c < du) & notbrk
    X["W1"] = w1 & ~w1.shift(1, fill_value=False)
    X["W2"] = base & (c > e20) & (c.shift(1) <= e20.shift(1)) & notbrk
    X["W3"] = base & (c > e50) & (c.shift(1) <= e50.shift(1)) & notbrk
    return X


def size_preview(ref_entry: float, atr: float, equity: float, risk_mult: float = 1.0,
                 risk_pct: Optional[float] = None) -> Dict[str, float]:
    """risk_pct = absolute fraction of equity risked (manual override); default = base risk x tier multiple."""
    st, rk = settings.cfg()["strategy"], settings.cfg()["risk"]
    stop_dist = max(atr * float(st["atr_multiplier"]), ref_entry * float(st["stop_floor_pct"]))
    rp = float(risk_pct) if risk_pct is not None else float(rk["risk_pct"]) * risk_mult
    raw = equity * rp / stop_dist if stop_dist > 0 else 0
    cap = equity * float(rk["max_position_value_pct"]) / ref_entry if ref_entry > 0 else 0
    sh = int(max(min(raw, cap, float(rk["max_shares"])), 0))
    return {"stop_price": ref_entry - stop_dist, "stop_dist": stop_dist, "stop_pct": stop_dist / ref_entry,
            "est_shares": sh, "est_risk": sh * stop_dist, "est_notional": sh * ref_entry, "risk_mult": risk_mult,
            "risk_pct": rp}


SETUP_ORDER = ["BRK", "W1", "W3", "W2"]                      # primary label when several fire on the same day


def tier_of(setup: str, dist200: float, days_s2: float, price: float, adv: float) -> Optional[str]:
    """A/B/W are tradable tiers, C is info-only (other breakouts). Returns None = discard."""
    st = settings.cfg()["strategy"]
    T = st["tiers"]
    if price < float(st["min_price"]):
        return None
    d = -dist200
    if setup == "BRK":
        for k in ("A", "B"):
            t = T[k]
            if d >= float(t["min_dist_below_ema200"]) and days_s2 <= float(t["max_days_since_stage2"]):
                return k
        return "C" if st.get("show_tier_c", True) else None
    w = T["W"]
    if d >= float(w["min_dist_below_ema200"]) and days_s2 <= float(w["max_days_since_stage2"]) \
            and adv >= float(w.get("min_avg_dollar_vol", 0)):
        return "W"
    return None


def risk_mult_of(tier: str) -> float:
    t = settings.cfg()["strategy"]["tiers"].get(tier)
    return float(t["risk_mult"]) if t else 0.0


# ── per-ticker worker (top-level so Windows multiprocessing can pickle it) ──
def _metrics(X: pd.DataFrame, r: pd.Series, tags: List[str]) -> Dict[str, Any]:
    adv = float((X.close * X.volume).tail(20).mean())
    return {
        "setup": tags[0] if tags else None, "setup_tags": "+".join(tags),
        "stage": int(r["stage"]), "prev_stage": int(r["prev_stage"]), "close": float(r["close"]),
        "atr": float(r["atr14"]), "atr_pct": float(r["atr14"] / r["close"]),
        "ema10": float(r["ema_10"]), "ema20": float(r["ema_20"]), "ema50": float(r["ema_50"]),
        "ema200": float(r["ema_200"]), "dist200": float(r["dist200"]),
        "days_since_s2": int(r["days_since_s2"]) if r["days_since_s2"] == r["days_since_s2"] else 9999,
        "donchian_upper": float(r.get("donchian_upper_s1", np.nan)), "bb_lower": float(r.get("bb_lower", np.nan)),
        "bb_mid": float(r.get("bb_mid", np.nan)), "volume_ratio": float(r.get("volume_ratio", np.nan)),
        "rsi": float(r.get("rsi", np.nan)), "avg_dollar_vol": adv,
    }


def _tags(r: pd.Series) -> List[str]:
    return [k for k, on in (("BRK", bool(r["signal"])), ("W1", bool(r["W1"])), ("W3", bool(r["W3"])), ("W2", bool(r["W2"]))) if on]


def analyse(ticker: str, asof: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        df = _read(ticker)
        if df is not None and asof:
            df = df[df.date <= pd.Timestamp(asof)].reset_index(drop=True)
        if df is None or len(df) < MIN_BARS + 40:
            return None
        X = prepare(df)
        r = X.iloc[-1]
        out = {"ticker": ticker, "last_date": str(r["date"].date()), "stage": int(r["stage"])}
        tags = _tags(r)
        if tags:
            out["sig"] = _metrics(X, r, tags)
        return out
    except Exception as exc:                                  # noqa: BLE001
        return {"ticker": ticker, "error": str(exc)}


def latest_metrics(ticker: str) -> Optional[Dict[str, Any]]:
    """Current numbers for a watchlist ticker (latest cached bar), whether or not a setup fires today."""
    df = _read(ticker)
    if df is None or len(df) < MIN_BARS + 40:
        return None
    X = prepare(df)
    r = X.iloc[-1]
    m = _metrics(X, r, _tags(r))
    m["as_of"] = str(r["date"].date())
    m["stage_name"] = STAGE_NAMES.get(m["stage"], str(m["stage"]))
    return m


def refresh_watchlist() -> int:
    """Refresh unexecuted tradable / starred signals of the active session with the latest candles and re-size them."""
    sess = clock.status()["session_date"]
    rows = db.q("SELECT * FROM signals WHERE entry_date=? AND exec_status='none' AND (tier IN ('A','B','W') OR watch=1)", (sess,))
    eq = float(db.kv_get("equity", settings.cfg()["risk"]["default_equity"]))
    n = 0
    for r in rows:
        try:
            m = latest_metrics(r["ticker"])
        except Exception as exc:                              # noqa: BLE001
            log.warning("watchlist refresh %s: %s", r["ticker"], exc)
            continue
        if not m or m["as_of"] == r.get("as_of"):
            continue
        sp = size_preview(m["close"], m["atr"], eq, r["risk_mult"] or 1.0, risk_pct=r["risk_pct"])
        keys = ["stage", "prev_stage", "close", "atr", "atr_pct", "ema10", "ema20", "ema50", "ema200", "dist200", "days_since_s2",
                "donchian_upper", "bb_lower", "bb_mid", "volume_ratio", "rsi", "avg_dollar_vol", "stage_name", "as_of"]
        vals = {k: m[k] for k in keys}
        vals.update(live_setup=m["setup_tags"] or None, ref_entry=m["close"], stop_price=sp["stop_price"], stop_dist=sp["stop_dist"],
                    stop_pct=sp["stop_pct"], est_shares=sp["est_shares"], est_risk=sp["est_risk"], est_notional=sp["est_notional"])
        db.x(f"UPDATE signals SET {','.join(k + '=?' for k in vals)} WHERE id=?", list(vals.values()) + [r["id"]])
        n += 1
    return n


# ── scan ──────────────────────────────────────────────────────────────────────
def universe() -> List[str]:
    prod_cfg = load_production_config(settings.REPO_ROOT)
    eligible, data_set = resolve_universe(prod_cfg, settings.REPO_ROOT)
    have = {os.path.basename(p)[:-8] for p in glob.glob(str(settings.cache_dir() / "*.parquet"))}
    held = {r["ticker"] for r in db.q("SELECT DISTINCT ticker FROM positions WHERE status='open'")}
    tickers = sorted(((eligible & have) | (held & have)))
    if not tickers:                                           # no universe file anywhere: scan the whole cache
        log.warning("Universe empty after intersecting with cache -> scanning all cached tickers")
        tickers = sorted(have)
    lim = int(os.environ.get("DESK_SCAN_LIMIT", "0") or 0)   # smoke tests only
    return tickers[:lim] if lim else tickers


def run_scan(workers: Optional[int] = None, allow_stale: bool = False, asof: Optional[str] = None,
             as_session: bool = False) -> Dict[str, Any]:
    t0 = time.time()
    tickers = universe()
    workers = workers or int(settings.cfg()["data"].get("scan_workers", 4))
    log.info("Scanning %d tickers with %d workers | cache=%s", len(tickers), workers, settings.cache_dir())
    results: List[Dict[str, Any]] = []
    if workers > 1:
        try:
            with Pool(workers) as pool:
                for i, r in enumerate(pool.imap(partial(analyse, asof=asof), tickers, chunksize=8), 1):
                    if r:
                        results.append(r)
                    if i % 500 == 0:
                        log.info("  %d/%d", i, len(tickers))
        except Exception as exc:                              # noqa: BLE001  (e.g. spawn limits inside a server thread)
            log.warning("Worker pool failed (%s) -> falling back to a single process (slower)", exc)
            results = [r for r in (analyse(t, asof) for t in tickers) if r]
    else:
        results = [r for r in (analyse(t, asof) for t in tickers) if r]
    errors = [r for r in results if "error" in r]
    ok = [r for r in results if "error" not in r]
    if not ok:
        return {"ok": False, "error": "no data scanned", "errors": len(errors)}
    from collections import Counter
    sig_date = Counter(r["last_date"] for r in ok).most_common(1)[0][0]   # modal last bar: robust to a few odd tickers
    entry = clock.entry_date_for(sig_date)
    session = clock.active_session()
    if as_session:                         # TEST ONLY: treat these (older) signals as the current session's
        entry, allow_stale = session, True
        log.warning("TEST MODE: signals from %s relabelled as session %s", sig_date, session.date())
    stale_cache = entry < session and not (allow_stale or asof)
    n_old = sum(1 for r in ok if r["last_date"] < sig_date)

    equity = float(db.kv_get("equity", settings.cfg()["risk"]["default_equity"]))
    counts = {"A": 0, "B": 0, "W": 0, "C": 0}
    new_ids = []
    if not stale_cache:
        for r in ok:
            s = r.get("sig")
            if not s or r["last_date"] != sig_date:
                continue
            tier = tier_of(s["setup"], s["dist200"], s["days_since_s2"], s["close"], s["avg_dollar_vol"])
            if tier is None:
                continue
            rm = risk_mult_of(tier)
            sp = size_preview(s["close"], s["atr"], equity, rm if rm > 0 else 1.0)
            row = {"signal_date": sig_date, "entry_date": str(entry.date()), "ticker": r["ticker"], "tier": tier,
                   "stage_name": STAGE_NAMES.get(s["stage"], str(s["stage"])), "ref_entry": s["close"], **s, **sp,
                   "sig_close": s["close"], "as_of": sig_date, "live_setup": s["setup_tags"], "risk_pct_default": sp["risk_pct"]}
            new_ids.append(db.upsert_signal(row))
            counts[tier] += 1

    refreshed = 0
    if not stale_cache:
        db.kv_set("last_scan", {"signal_date": sig_date})        # board date must be known before the watchlist refresh / roll
        from desk import rules
        rules.roll_forward()
        refreshed = refresh_watchlist()

    # Stage-9 exit monitoring for desk-held tickers (executed at the open; no approval needed, same as the old system)
    held = {r["ticker"] for r in db.q("SELECT DISTINCT ticker FROM positions WHERE status='open'")}
    exits = []
    for r in ok:
        if r["ticker"] in held and r["last_date"] == sig_date and r["stage"] == 9:
            db.x("INSERT OR REPLACE INTO pending_exits(ticker,reason,created_at) VALUES(?,?,?)",
                 (r["ticker"], "stage9_exit", db.now_iso()))
            exits.append(r["ticker"])

    summary = {"ok": not stale_cache, "signal_date": sig_date, "entry_date": str(entry.date()),
               "session": str(session.date()), "stale_cache": stale_cache, "tickers": len(tickers),
               "scanned": len(ok), "errors": len(errors), "tickers_with_older_last_bar": n_old,
               "tier_counts": counts, "watchlist_refreshed": refreshed, "stage9_exits_queued": exits, "seconds": round(time.time() - t0, 1),
               "ran_at": db.now_iso()}
    db.kv_set("last_scan", summary)
    db.audit("system", "scan", summary)
    log.info("Scan done: %s", summary)
    return summary


# ── chart payload ─────────────────────────────────────────────────────────────
def chart_payload(ticker: str, asof: Optional[str] = None, bars: int = 260) -> Dict[str, Any]:
    df = _read(ticker)
    if df is None:
        raise FileNotFoundError(ticker)
    if asof:
        df = df[df.date <= pd.Timestamp(asof)].reset_index(drop=True)
    X = prepare(df).tail(bars)

    def line(col: str) -> List[Dict[str, Any]]:
        if col not in X.columns:
            return []
        return [{"time": str(d.date()), "value": float(v)} for d, v in zip(X.date, X[col]) if v == v]

    X["dc_up"] = X.high.rolling(20).max()                    # TradingView Donchian(20): includes the current bar
    X["dc_lo"] = X.low.rolling(20).min()
    X["dc_mid"] = (X.dc_up + X.dc_lo) / 2
    sigs = X[X.signal]
    s2 = X[X.stage == 2]
    return {
        "ticker": ticker,
        "candles": [{"time": str(d.date()), "open": float(o), "high": float(h), "low": float(l), "close": float(c)}
                    for d, o, h, l, c in zip(X.date, X.open, X.high, X.low, X.close)],
        "volume": [{"time": str(d.date()), "value": float(v), "color": "#26a69a55" if c >= o else "#ef535055"}
                   for d, v, o, c in zip(X.date, X.volume, X.open, X.close)],
        "lines": {"EMA 10": line("ema_10"), "EMA 20": line("ema_20"), "EMA 50": line("ema_50"), "EMA 200": line("ema_200"),
                  "Donchian upper": line("dc_up"), "Donchian lower": line("dc_lo"), "Donchian basis": line("dc_mid"),
                  "Breakout level (prior 20d high)": line("donchian_upper_s1"),
                  "BB upper": line("bb_upper"), "BB mid": line("bb_mid"), "BB lower": line("bb_lower")},
        "signals": [str(d.date()) for d in sigs.date],
        "stage2_bars": [str(d.date()) for d in s2.date],
        "stage": [{"time": str(d.date()), "stage": int(s)} for d, s in zip(X.date, X.stage)][-5:],
        "last_date": str(X.date.iloc[-1].date()),
    }
