# tools/parity_replay.py
"""
APEX parity replay -- production SignalGenerator vs backtest raw signals.

Read-only. Replays production signal logic on backtest OHLCV truncated to the
last N bars (production cache depth), compares against
ALGO-Stocks output/signals/raw_signals_all.parquet.

Per ticker, evaluates:
  - every backtest signal date (recall: does production also fire?)
  - random non-signal dates (precision: does production fire spuriously?)

Usage:
  python tools/parity_replay.py --algo-root C:/Users/ravil/PycharmProjects/ALGO-Stocks \
      --tickers 60 --window 360 --since 2024-06-01 --out outputs/parity_replay.csv
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils.config_loader import (  # noqa: E402
    load_indicator_config, load_production_config, load_stage_config,
)
from prod.signals.signal_generator import SignalGenerator  # noqa: E402

logging.disable(logging.CRITICAL)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--algo-root", required=True)
    ap.add_argument("--tickers", type=int, default=60)
    ap.add_argument("--window", type=int, default=360)
    ap.add_argument("--since", default="2024-06-01")
    ap.add_argument("--neg-per-ticker", type=int, default=12)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="outputs/parity_replay.csv")
    a = ap.parse_args()

    algo = Path(a.algo_root)
    sig = pd.read_parquet(
        algo / "output/signals/raw_signals_all.parquet",
        columns=["ticker", "signal_date"],
    )
    sig = sig[sig.signal_date >= a.since]
    all_t = sorted(sig.ticker.unique())
    rng = random.Random(a.seed)
    tickers = rng.sample(all_t, min(a.tickers, len(all_t)))

    gen = SignalGenerator(
        load_production_config(ROOT), load_indicator_config(ROOT),
        load_stage_config(ROOT),
        Path(tempfile.mkdtemp()),
    )
    rows = []
    for k, t in enumerate(tickers, 1):
        px = pd.read_parquet(
            algo / f"data/raw/prices_daily/twelvedata/parquets/{t}.parquet"
        )
        px["date"] = pd.to_datetime(px["date"])
        px = px.sort_values("date").reset_index(drop=True)
        pos_dates = set(sig[sig.ticker == t].signal_date)
        cand = [d for d in px.date[px.date >= a.since] if d not in pos_dates]
        neg_dates = rng.sample(cand, min(a.neg_per_ticker, len(cand)))
        for d, truth in [(d, 1) for d in pos_dates] + [(d, 0) for d in neg_dates]:
            idx = px.index[px.date == d]
            if len(idx) == 0:
                continue
            win = px.iloc[max(0, idx[0] + 1 - a.window): idx[0] + 1].copy()
            # fresh stage2 memory each call = worst-case cold start
            gen.state_dir = Path(tempfile.mkdtemp())
            try:
                fired = gen.generate(t, win) is not None
            except Exception:
                fired = False
            rows.append({"ticker": t, "date": d, "backtest": truth,
                         "prod": int(fired), "bars": len(win)})
        print(f"[{k}/{len(tickers)}] {t}", flush=True)

    df = pd.DataFrame(rows)
    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    pos, neg = df[df.backtest == 1], df[df.backtest == 0]
    print(f"\nbacktest signals tested : {len(pos)}")
    print(f"recall  (prod fires)    : {pos['prod'].mean():.3f}")
    print(f"non-signal days tested  : {len(neg)}")
    print(f"false positive rate     : {neg['prod'].mean():.4f}")
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
