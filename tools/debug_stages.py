# tools/debug_stages.py
"""
Debug script — shows current stage + key indicator values for all tickers.
Run: python tools/debug_stages.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import warnings
warnings.filterwarnings("ignore")

import yfinance as yf
import pandas as pd

from core.features.technicals.pipeline import IndicatorConfig, apply_indicators
from core.stages.stage_classifier import StageClassifier, STAGE_NAMES
from core.utils.config_loader import (
    load_production_config,
    load_indicator_config,
    load_stage_config,
)

prod_cfg    = load_production_config(ROOT)
ind_cfg     = IndicatorConfig.from_dict(load_indicator_config(ROOT))
stage_cfg   = load_stage_config(ROOT)
state_dir   = ROOT / "state"
tickers     = prod_cfg["universe"]["tickers"]
lookback    = prod_cfg["universe"]["lookback_days"]

print(f"\n{'TICKER':<8} {'STAGE':<5} {'STAGE NAME':<22} {'CLOSE':>8} {'EMA10':>8} {'EMA20':>8} {'EMA50':>8} {'VOL_SURGE':<10} {'S2_MEM'}")
print("-" * 110)

stage_counts = {}

for ticker in tickers:
    try:
        raw = yf.download(ticker, period=f"{lookback}d", auto_adjust=True, progress=False)
        if raw.empty or len(raw) < 50:
            print(f"{ticker:<8} NO DATA")
            continue

        # Flatten multi-level columns if present
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [c[0].lower() for c in raw.columns]
        else:
            raw.columns = [c.lower() for c in raw.columns]

        raw = raw.reset_index()
        raw.rename(columns={"date": "date", "Date": "date"}, inplace=True)
        raw["date"] = pd.to_datetime(raw["date"])

        df = apply_indicators(raw, ind_cfg)
        classifier = StageClassifier(ticker, state_dir, stage_cfg)
        df = classifier.classify(df)

        last = df.iloc[-1]
        stage = int(last.get("stage", 0))
        stage_name = STAGE_NAMES.get(stage, "Unknown")
        close = float(last.get("close", 0))
        ema10 = float(last.get("ema_10", 0))
        ema20 = float(last.get("ema_20", 0))
        ema50 = float(last.get("ema_50", 0))
        vol_surge = bool(last.get("volume_surge", False))
        s2_mem = bool(last.get("stage2_memory", False))

        stage_counts[stage] = stage_counts.get(stage, 0) + 1

        marker = " <<< SIGNAL" if stage == 7 and s2_mem else ""
        print(f"{ticker:<8} {stage:<5} {stage_name:<22} {close:>8.2f} {ema10:>8.2f} {ema20:>8.2f} {ema50:>8.2f} {str(vol_surge):<10} {str(s2_mem)}{marker}")

    except Exception as e:
        print(f"{ticker:<8} ERROR: {e}")

print("-" * 110)
print("\nStage distribution:")
for s in sorted(stage_counts):
    print(f"  Stage {s} ({STAGE_NAMES.get(s, '?')}): {stage_counts[s]} ticker(s)")

print(f"\nStage 7 (actionable signals): {stage_counts.get(7, 0)}")
