# research/dislocation_study/01_build_merged_features.py
"""
STEP 1 -- Merge candles and rebuild indicators / stages / signals.

Inputs (read-only):
  - Backtest candles   : ALGO-Stocks data/raw/prices_daily/twelvedata/parquets  (to 2026-01-30)
  - Work PC cache      : APEX data/raw/work_pc_cache/.../parquets               (to ~2026-10)
Method:
  - Per ticker: use the Work PC cache for all its dates and prepend backtest bars that
    pre-date it. If both overlap, close prices are compared; if the median ratio differs
    by > 0.3% (split/adjustment drift) the older backtest bars are rescaled to match.
  - Indicators + stage classification use the PRODUCTION code (APEX core/), which was
    validated to reproduce 100% of backtest signals on full history.
  - Backtest stage floor: first 259 bars are forced to Stage 1.
  - Signal = Stage 6/7 transition (from non-6/7) with Stage 2 seen on an earlier bar.
Output: output/shards/s*.parquet (resumable; delete the folder to rebuild from scratch).

Run:  python 01_build_merged_features.py
"""
from __future__ import annotations

import glob
import os
import sys
import time
import warnings
from multiprocessing import Pool

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import yaml

import study_config as C

sys.path.insert(0, str(C.APEX_ROOT))
from core.features.technicals.pipeline import IndicatorConfig, apply_indicators  # noqa: E402
from core.stages.stage_classifier import classify_stages  # noqa: E402

warnings.filterwarnings("ignore")

_IND = yaml.safe_load((C.APEX_ROOT / "config" / "indicators.yaml").read_text(encoding="utf-8"))
_STG = yaml.safe_load((C.APEX_ROOT / "config" / "stages.yaml").read_text(encoding="utf-8"))
ICFG = IndicatorConfig.from_dict(_IND)


def load(p: str) -> pd.DataFrame:
    t = pq.read_table(p).to_pandas()
    t["date"] = pd.to_datetime(t["date"]).dt.tz_localize(None)
    return (t[["date", "open", "high", "low", "close", "volume"]]
            .sort_values("date").drop_duplicates("date").reset_index(drop=True))


def atr14(df: pd.DataFrame) -> pd.Series:
    h, l, c = df.high.astype(float), df.low.astype(float), df.close.astype(float)
    p = c.shift(1)
    tr = pd.concat([h - l, (h - p).abs(), (l - p).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()


def process(t: str):
    try:
        wcp = C.WORK_CACHE / f"{t}.parquet"
        btp = C.BT_CANDLES / f"{t}.parquet"
        w = load(str(wcp)) if wcp.exists() else None
        b = load(str(btp)) if btp.exists() else None
        if w is None and b is None:
            return None
        flag = ""
        if w is not None and b is not None:
            ov = w.merge(b, on="date", suffixes=("_w", "_b"))
            if len(ov) > 20:
                med = float((ov.close_w / ov.close_b).median())
                if abs(med - 1) > 0.003:
                    flag = f"rescaled{med:.4f}"
                    for c in ["open", "high", "low", "close"]:
                        b[c] = b[c] * med
                    b["volume"] = b["volume"] / med
            pre = b[b.date < w.date.min()]
            df = pd.concat([pre, w]) if len(pre) else w.copy()
            df = df.reset_index(drop=True)
        else:
            df = (w if w is not None else b).copy()
        df = df.dropna(subset=["close"]).reset_index(drop=True)
        if len(df) < 300:
            return None

        X = apply_indicators(df.copy(), ICFG)
        S = classify_stages(X, _STG)
        X["stage"] = S["stage"].values
        n = len(X)
        X.loc[: C.MIN_HISTORY_BARS - 2, "stage"] = 1          # backtest 260-bar floor
        X["atr"] = atr14(X)

        s2 = (X.stage == 2).astype(int)
        X["s2_before"] = s2.shift(1).fillna(0).cummax().astype(bool)
        prev = X.stage.shift(1).fillna(-1).astype(int)
        X["prev_stage"] = prev
        X["signal"] = X.stage.isin([6, 7]) & ~prev.isin([6, 7]) & X.s2_before

        idx = np.arange(n)
        last2 = pd.Series(np.where(X.stage == 2, idx, np.nan)).ffill().shift(1)
        X["days_since_s2"] = idx - last2.values

        # dislocation features (all point-in-time: use data up to the signal bar)
        X["dist200"] = X.close / X.ema_200 - 1
        X["min_dist200_60"] = X.dist200.rolling(60, min_periods=20).min()
        X["dd120"] = X.close / X.close.rolling(120, min_periods=60).max() - 1
        X["ret20"] = X.close / X.close.shift(20) - 1
        X["atr_pct"] = X.atr / X.close
        X["ticker"] = t
        X["flag"] = flag
        keep = ["ticker", "date", "open", "high", "low", "close", "volume",
                "ema_10", "ema_20", "ema_50", "ema_200", "bb_lower", "bb_mid",
                "donchian_upper_s1", "volume_ratio", "rsi", "atr", "atr_pct",
                "stage", "prev_stage", "signal", "s2_before", "days_since_s2",
                "dist200", "min_dist200_60", "dd120", "ret20", "flag"]
        return X[[k for k in keep if k in X.columns]]
    except Exception:
        return None


def main() -> None:
    C.SHARDS.mkdir(parents=True, exist_ok=True)
    wc = {os.path.basename(p)[:-8] for p in glob.glob(str(C.WORK_CACHE / "*.parquet"))}
    bt = {os.path.basename(p)[:-8] for p in glob.glob(str(C.BT_CANDLES / "*.parquet"))}
    tick = sorted(wc | bt)
    lim = int(os.environ.get("STUDY_LIMIT", "0") or 0)   # quick smoke test: STUDY_LIMIT=200
    if lim:
        tick = tick[:lim]
    print(f"[INFO] tickers: work_cache={len(wc)} backtest={len(bt)} union={len(tick)}")

    have = set()
    for f in glob.glob(str(C.SHARDS / "s*.parquet")):
        have |= set(pq.read_table(f, columns=["ticker"]).column("ticker").to_pylist())
    todo = [t for t in tick if t not in have]
    print(f"[INFO] already done={len(have)} remaining={len(todo)}")
    if not todo:
        print("[OK] nothing to do")
        return

    k = len(glob.glob(str(C.SHARDS / "s*.parquet")))
    buf, done, t0 = [], 0, time.time()
    workers = int(os.environ.get("STUDY_WORKERS", "0") or 0) or max(1, min(4, (os.cpu_count() or 2) // 2))
    print(f"[INFO] workers={workers}")
    with Pool(workers) as pool:
        for r in pool.imap(process, todo, chunksize=4):
            done += 1
            if r is not None:
                buf.append(r)
            if len(buf) >= 150:
                pd.concat(buf).to_parquet(C.SHARDS / f"s{k:03d}.parquet")
                k += 1
                buf = []
            if done % 300 == 0:
                print(f"  {done}/{len(todo)}  {time.time() - t0:.0f}s", flush=True)
    if buf:
        pd.concat(buf).to_parquet(C.SHARDS / f"s{k:03d}.parquet")
    print(f"[DONE] {time.time() - t0:.0f}s -> {C.SHARDS}")


if __name__ == "__main__":
    main()
