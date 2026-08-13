# prod/signals/signal_generator.py
"""
APEX Signal Generator — exact replication of backtest 09A signal logic.

Signal detection rules (from 09A _extract_signals_for_ticker):
  1. TRANSITION detection: stage must enter Stage 6 or 7 FROM a non-entry state.
     A ticker already in Stage 7 for 5 consecutive days generates exactly ONE signal
     (on day 1 of Stage 7). This prevents double-counting.
  2. Stage 2 prerequisite (design lock): stage2_ever_before must be True on
     a day STRICTLY BEFORE the signal bar (shift(1) + cummax — point-in-time safe).
  3. ATR(14) computed on signal bar for stop sizing.
  4. Entry is at next bar's open (not signal close).
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import numpy as np

from core.features.technicals.pipeline import IndicatorConfig, apply_indicators
from core.stages.stage_classifier import STAGE_NAMES, StageClassifier

logger = logging.getLogger("signal_generator")

# Entry stages — matches backtest config: entry_stages: [6, 7]
ENTRY_STAGES = {6, 7}


def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Wilder's ATR — exact backtest formula (09A _compute_atr).
    True Range = max(H-L, |H-prevC|, |L-prevC|)
    Smoothing  = EWM alpha=1/period (Wilder's method)
    """
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    close = df["close"].astype(float)
    prev  = close.shift(1)

    tr = pd.concat([
        (high - low),
        (high - prev).abs(),
        (low  - prev).abs(),
    ], axis=1).max(axis=1)

    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


class SignalGenerator:
    """
    EOD signal generator — mirrors backtest 09A logic exactly.

    For each ticker:
      1. Apply indicators (pipeline.py — unchanged from backtest)
      2. Classify stages (stage_classifier.py — unchanged from backtest)
      3. Detect TRANSITIONS into Stage 6 or 7
      4. Apply Stage 2 prerequisite (shift(1) cummax — point-in-time safe)
    """

    def __init__(
        self,
        prod_cfg: Dict[str, Any],
        indicator_cfg_dict: Dict[str, Any],
        stage_cfg_dict: Dict[str, Any],
        state_dir: Path,
    ) -> None:
        self.prod_cfg   = prod_cfg
        self.ind_cfg    = IndicatorConfig.from_dict(indicator_cfg_dict)
        self.stage_cfg  = stage_cfg_dict
        self.state_dir  = Path(state_dir)
        self._atr_period = prod_cfg.get("stop", {}).get("atr_period", 14)
        self._require_stage2 = prod_cfg.get("entry", {}).get("require_stage2_history", True)

    def generate(
        self,
        ticker: str,
        df: pd.DataFrame,
    ) -> Optional[Dict[str, Any]]:
        """
        Run signal detection for one ticker on the full OHLCV history.

        Returns signal dict if a Stage 6/7 TRANSITION is detected on the
        latest bar (today's close), else None.

        Signal dict keys:
          ticker, signal_date, stage, stage_name, stage2_memory,
          close, atr, entry_open (NaN — filled at execution),
          stop_price (NaN — recalculated at entry_open)
        """
        if df.empty or len(df) < 50:
            logger.debug(f"{ticker}: insufficient bars ({len(df)})")
            return None

        # ── Apply indicators ──────────────────────────────────────────────────
        df = apply_indicators(df, self.ind_cfg)

        # ── Classify stages with expanding Stage 2 memory ─────────────────────
        classifier = StageClassifier(ticker, self.state_dir, self.stage_cfg)
        df = classifier.classify(df)

        if len(df) < 2:
            return None

        # ── ATR(14) — Wilder's method (matches 09A _compute_atr) ─────────────
        df["atr_14"] = _compute_atr(df, period=self._atr_period)

        # ── Stage 2 prerequisite — point-in-time safe (matches 09A shift logic) ─
        # stage2_ever_before[i] = True iff Stage 2 appeared on any day j < i
        # The signal bar itself is NOT counted as "having Stage 2 history"
        stage2_flag = (df["stage"] == 2).astype(int)
        df["stage2_ever_before"] = (
            stage2_flag.shift(1).fillna(0).astype(int).cummax().astype(bool)
        )

        # ── Transition detection (matches 09A is_signal logic) ────────────────
        df["prev_stage"] = df["stage"].shift(1).fillna(-1).astype(int)
        df["is_entry_stage"]  = df["stage"].isin(ENTRY_STAGES)
        df["was_entry_stage"] = df["prev_stage"].isin(ENTRY_STAGES)
        df["is_transition"]   = df["is_entry_stage"] & ~df["was_entry_stage"]

        # ── Check today's bar (last row) ──────────────────────────────────────
        last = df.iloc[-1]
        is_transition = bool(last.get("is_transition", False))

        if not is_transition:
            return None   # not a fresh Stage 6/7 entry today

        # ── Stage 2 prerequisite gate ─────────────────────────────────────────
        if self._require_stage2:
            stage2_before = bool(last.get("stage2_ever_before", False))
            if not stage2_before:
                logger.debug(f"{ticker}: Stage {int(last['stage'])} transition — no Stage 2 history (design lock)")
                return None

        stage       = int(last["stage"])
        signal_date = pd.Timestamp(last.get("date", pd.Timestamp.now())).normalize()
        close       = float(last.get("close", 0.0))
        atr         = float(last.get("atr_14", 0.0)) if not pd.isna(last.get("atr_14", float("nan"))) else 0.0

        # ── EOD stop estimate (recalculated precisely at next-day open) ────────
        stop_cfg    = self.prod_cfg.get("stop", {})
        atr_mult    = float(stop_cfg.get("atr_multiplier", 2.0))
        floor_pct   = float(stop_cfg.get("floor_pct", 0.005))
        stop_est    = close - atr * atr_mult
        stop_floor  = close * (1 - floor_pct)
        stop_price_eod = max(stop_est, stop_floor)

        logger.info(
            f"SIGNAL: {ticker} | date={signal_date.date()} | "
            f"stage={'7-Confirmed' if stage==7 else '6-Breakout'} | "
            f"close={close:.2f} | atr={atr:.4f} | stop_est={stop_price_eod:.2f} | "
            f"transition_from=Stage{int(last['prev_stage'])}"
        )

        return {
            "ticker":         ticker,
            "signal_date":    signal_date,
            "stage":          stage,
            "stage_name":     STAGE_NAMES[stage],
            "stage2_memory":  True,
            "close":          close,
            "atr":            atr,
            "entry_open":     float("nan"),   # filled at next-day execution
            "stop_price":     float("nan"),   # recalculated at entry_open
            "stop_price_eod": stop_price_eod,
            "direction":      "long",
        }

    def generate_all(
        self,
        universe_data: Dict[str, pd.DataFrame],
    ) -> List[Dict[str, Any]]:
        """
        Run signal generation for entire universe.
        Returns list of signal dicts (only triggered transitions).
        """
        signals = []
        for ticker, df in universe_data.items():
            try:
                sig = self.generate(ticker, df)
                if sig is not None:
                    signals.append(sig)
            except Exception as exc:
                logger.error(f"{ticker}: signal generation error — {exc}", exc_info=True)
        logger.info(f"Signals: {len(signals)} / {len(universe_data)} tickers")
        return signals
