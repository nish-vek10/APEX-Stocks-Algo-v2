# tools/broker_suitability.py
"""
APEX -- Broker suitability diagnostic (READ-ONLY, DEMO-ONLY).

Evaluates whether a candidate MT5 broker lists/can trade the stock universe
APEX needs, WITHOUT touching the running IC Markets demo:

  - Uses its OWN explicit terminal path (a separate MT5 install) -- never
    reads MT5_LOGIN / MT5_PASSWORD / MT5_SERVER / MT5_TERMINAL_PATH to connect.
  - REFUSES to run if --terminal-path equals the IC terminal path from .env.
  - REFUSES to run on a non-demo account.
  - Password is prompted (getpass), never stored or logged.
  - Sends NO orders. Uses order_calc_margin() and order_check() (dry-run
    validation only) -- order_send() is never called.
  - Writes ONLY under tools/output/broker_eval/<name>/ -- never overwrites the
    IC catalogue files (mt5_stock_catalogue_latest.*) or config/mt5_symbol_map.yaml.

Checks:
  1. Coverage: share of the eligible universe (state/universe_active.json,
     else production.yaml tickers) found on the broker; plus coverage of
     tickers APEX has actually traded (trade_log.json / positions.json).
  2. Tradability: trade_mode FULL, min volume / step (fractional shares?),
     contract size, margin per position, stops level (can we attach SL?).
  3. Sampled live quotes: spread % (only meaningful while market is open),
     order_check() of a min-volume BUY with a 5% SL (retcode only).
  4. Comparison against IC Markets mapping coverage (config/mt5_symbol_map.yaml).

One-time setup (per PC): copy config/brokers.example.yaml -> config/brokers.yaml
(git-ignored) and fill in terminal_path / login / server (/ password, or leave
"" to be prompted). Then:

    python tools/broker_suitability.py --broker pepperstone [--sample 300]

Run while the US market is open and NOT during the IC 09:31 ET execution window.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from core.utils.universe_state import resolve_universe  # noqa: E402

try:
    import MetaTrader5 as mt5
except ImportError:
    print("MetaTrader5 not installed. Run: pip install MetaTrader5")
    sys.exit(1)

# base ticker + optional exchange suffix + optional "-24" / other variant tails
_SUFFIX_RE = re.compile(r"^([A-Z][A-Z0-9\-]*?)(\.(NAS|NYSE|US|NYS|OTC|N|O|A|NASDAQ|NYSE_ARCA))?(-24|\.24|_24)?$")
_STOCK_PATH_KEYWORDS = ("stock", "share", "equit")


def _norm_path(p: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def connect(args) -> None:
    ic_path = os.environ.get("MT5_TERMINAL_PATH", "").strip()
    if not ic_path:
        print("[STOP] MT5_TERMINAL_PATH is empty in .env -- the live IC scheduler attaches to "
              "'whatever terminal is default/last used'. Launching another terminal could hijack it. "
              "Set MT5_TERMINAL_PATH to the IC terminal64.exe first.")
        sys.exit(2)
    if _norm_path(args.terminal_path) == _norm_path(ic_path):
        print("[STOP] --terminal-path equals the IC Markets terminal path. Use a separate install.")
        sys.exit(2)
    if not Path(args.terminal_path).exists():
        print(f"[STOP] terminal not found: {args.terminal_path}")
        sys.exit(2)

    pwd = args.password or getpass.getpass(
        f"Password for {args.login} @ {args.server} (not stored): "
    )
    if not mt5.initialize(path=args.terminal_path, login=int(args.login), password=pwd,
                          server=args.server, timeout=60000):
        print(f"[ERROR] initialize failed: {mt5.last_error()}")
        sys.exit(1)

    info = mt5.account_info()
    if info is None or info.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        print("[STOP] account is not DEMO (or unreadable). Refusing to continue.")
        mt5.shutdown()
        sys.exit(2)
    print(f"[OK] connected: login={info.login} server={info.server} company={info.company} "
          f"leverage=1:{info.leverage} currency={info.currency} equity={info.equity:.2f}")


def base_of(name: str):
    m = _SUFFIX_RE.match(name)
    return m.group(1) if m else None


def variant_rank(name: str) -> int:
    """Prefer standard-hours listing over 24h variant."""
    return 1 if re.search(r"(-24|\.24|_24)$", name) else 0


def traded_tickers() -> set[str]:
    out: set[str] = set()
    for f in ("trade_log.json", "positions.json"):
        p = ROOT / "state" / f
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            items = d.values() if isinstance(d, dict) else d
            out |= {str(x["ticker"]).upper() for x in items if isinstance(x, dict) and x.get("ticker")}
        except Exception:
            pass
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", required=True,
                    help="profile name in config/brokers.yaml, e.g. pepperstone")
    ap.add_argument("--sample", type=int, default=300, help="tickers to quote-test")
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    cfg_path = ROOT / "config" / "brokers.yaml"
    if not cfg_path.exists():
        print("[STOP] config/brokers.yaml missing. Copy config/brokers.example.yaml "
              "to config/brokers.yaml and fill in your broker details (one time).")
        sys.exit(2)
    profiles = (yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}).get("brokers", {})
    prof = profiles.get(args.broker)
    if not prof:
        print(f"[STOP] broker '{args.broker}' not in config/brokers.yaml. Found: {list(profiles)}")
        sys.exit(2)
    args.name = args.broker
    args.terminal_path = str(prof.get("terminal_path", ""))
    args.login = str(prof.get("login", "0"))
    args.server = str(prof.get("server", ""))
    args.password = str(prof.get("password", "") or "")
    if not args.terminal_path or args.login in ("", "0") or not args.server:
        print("[STOP] fill terminal_path, login and server for this broker in config/brokers.yaml")
        sys.exit(2)

    out_dir = ROOT / "tools" / "output" / "broker_eval" / args.name
    out_dir.mkdir(parents=True, exist_ok=True)

    prod_cfg = yaml.safe_load((ROOT / "config" / "production.yaml").read_text(encoding="utf-8"))
    eligible, _ = resolve_universe(prod_cfg, ROOT)
    traded = traded_tickers()
    ic_map = yaml.safe_load((ROOT / "config" / "mt5_symbol_map.yaml").read_text(encoding="utf-8")).get("symbols", {})
    ic_cov = {t for t in eligible if ic_map.get(t)}

    connect(args)
    try:
        syms = mt5.symbols_get() or []
        print(f"[INFO] total symbols on broker: {len(syms)}")

        path_counts: dict[str, int] = {}
        for s in syms:
            top = (s.path or "(none)").split("\\")[0]
            path_counts[top] = path_counts.get(top, 0) + 1

        # candidate stock symbols -> best listing per base ticker
        best: dict[str, object] = {}
        for s in syms:
            path = (s.path or "").lower()
            b = base_of(s.name)
            if not b:
                continue
            is_stock = any(k in path for k in _STOCK_PATH_KEYWORDS) or ("." in s.name)
            if not is_stock:
                continue
            cur = best.get(b)
            if cur is None or variant_rank(s.name) < variant_rank(cur.name):
                best[b] = s

        found = {t for t in eligible if t in best}
        missing = sorted(eligible - found)

        rows = []
        for t in sorted(eligible):
            s = best.get(t)
            rows.append({
                "ticker": t,
                "found": s is not None,
                "symbol": s.name if s else "",
                "path": s.path if s else "",
                "trade_mode": s.trade_mode if s else None,
                "tradable_full": bool(s and s.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL),
                "contract_size": s.trade_contract_size if s else None,
                "vol_min": s.volume_min if s else None,
                "vol_step": s.volume_step if s else None,
                "vol_max": s.volume_max if s else None,
                "stops_level": s.trade_stops_level if s else None,
                "fill_modes": s.filling_mode if s else None,
                "swap_long": s.swap_long if s else None,
                "traded_by_apex": t in traded,
                "on_ic_markets": t in ic_cov,
                "bid": None, "ask": None, "spread_pct": None,
                "margin_1lot": None, "order_check_retcode": None, "order_check_comment": "",
            })
        df = pd.DataFrame(rows).set_index("ticker")

        # --- sampled quote / margin / order_check (dry-run only) -------------
        rng = random.Random(args.seed)
        pool = sorted(found)
        must = sorted(found & traded)
        sample = list(dict.fromkeys(must + rng.sample(pool, min(args.sample, len(pool)))))
        print(f"[INFO] quote-testing {len(sample)} tickers (traded-by-APEX first)...")
        for t in sample:
            s = best[t]
            name = s.name
            was_visible = s.visible
            try:
                if not was_visible:
                    mt5.symbol_select(name, True)
                tick = mt5.symbol_info_tick(name)
                if tick and tick.bid > 0 and tick.ask > 0:
                    mid = (tick.bid + tick.ask) / 2
                    df.loc[t, ["bid", "ask"]] = [tick.bid, tick.ask]
                    df.loc[t, "spread_pct"] = round((tick.ask - tick.bid) / mid * 100, 4)
                    vol = max(s.volume_min, 1.0 if s.volume_step >= 1 else s.volume_min)
                    m = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, name, vol, tick.ask)
                    df.loc[t, "margin_1lot"] = m
                    # Pick a filling mode the symbol actually supports
                    # (bitmask: 1=FOK, 2=IOC; neither -> RETURN). A hard-coded
                    # mode gave false retcode 10030 on 307/312 symbols.
                    fm = int(s.filling_mode)
                    if fm & 1:
                        fill = mt5.ORDER_FILLING_FOK
                    elif fm & 2:
                        fill = mt5.ORDER_FILLING_IOC
                    else:
                        fill = mt5.ORDER_FILLING_RETURN
                    req = {
                        "action": mt5.TRADE_ACTION_DEAL, "symbol": name, "volume": vol,
                        "type": mt5.ORDER_TYPE_BUY, "price": tick.ask,
                        "sl": round(tick.ask * 0.95, s.digits), "deviation": 20,
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": fill,
                    }
                    chk = mt5.order_check(req)      # DRY RUN: validates only, sends nothing
                    if chk is not None:
                        df.loc[t, "order_check_retcode"] = chk.retcode
                        df.loc[t, "order_check_comment"] = chk.comment
            finally:
                if not was_visible:
                    mt5.symbol_select(name, False)

        df.reset_index().to_csv(out_dir / "coverage_detail.csv", index=False)
        pd.DataFrame(sorted(path_counts.items(), key=lambda x: -x[1]),
                     columns=["path_category", "count"]).to_csv(out_dir / "path_breakdown.csv", index=False)
        pd.DataFrame({"missing_ticker": missing}).to_csv(out_dir / "missing_tickers.csv", index=False)

        q = df.dropna(subset=["spread_pct"])
        full = df[df.found]
        summary = {
            "broker": args.name,
            "asof_utc": datetime.now(timezone.utc).isoformat(),
            "eligible_universe": len(eligible),
            "found_on_broker": int(df.found.sum()),
            "coverage_pct": round(df.found.mean() * 100, 1),
            "tradable_full_pct_of_found": round(full.tradable_full.mean() * 100, 1) if len(full) else None,
            "ic_markets_mapped": len(ic_cov),
            "ic_coverage_pct": round(len(ic_cov) / max(1, len(eligible)) * 100, 1),
            "apex_traded_tickers": len(traded),
            "apex_traded_found_on_broker": int(df[df.traded_by_apex & df.found].shape[0]),
            "fractional_volume_pct": round((full.vol_step < 1).mean() * 100, 1) if len(full) else None,
            "quoted_sample": len(q),
            "median_spread_pct": round(float(q.spread_pct.median()), 4) if len(q) else None,
            "p90_spread_pct": round(float(q.spread_pct.quantile(0.9)), 4) if len(q) else None,
            "order_check_retcodes": df.order_check_retcode.value_counts(dropna=True).to_dict(),
            "caveat": "spread/order_check only meaningful if US market was OPEN during the run",
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

        print("\n" + "=" * 60)
        for k, v in summary.items():
            print(f"  {k:32s}: {v}")
        print("=" * 60)
        print(f"Outputs: {out_dir}")
        print(f"Missing sample: {missing[:30]}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
