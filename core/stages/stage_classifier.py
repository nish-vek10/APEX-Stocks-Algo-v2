# core/stages/stage_classifier.py
"""
Stage classifier — EXACT replication of algo-stocks backtest stage_classifier.py.

Source: stages/stage_classifier.py (classify_stage function)
Backtest run: universe_baseline_v1_20260224_2310 | PF 2.26 | E[R] 0.63

Stage evaluation order (matches backtest priority exactly):
    2 → 3 → 4 → 5 → 7 → 6 → 9 → 8 → 1 (default)

Stage definitions:
    1  Not Eligible   — default fallback (no rule matched)
    2  Sharp Downtrend— below EMA200 + bearish stack + Donchian LOW breakdown + vol surge
    3  Downtrend      — below EMA200 + bearish stack (EMA10 < EMA20 < EMA50 < EMA200)
    4  Below Zone     — below EMA200 + close ≤ BB lower
    5  Lower Zone     — below EMA200 + close between BB lower and BB mid
    6  Breakout       — close > Donchian high (shifted-1) + EMA10 > EMA20
    7  Breakout Conf. — Stage 6 conditions + close > EMA50 (checked BEFORE Stage 6)
    8  In-Zone        — above EMA200
    9  In-Zone Fading — above EMA200 + close < EMA10

DESIGN LOCKS (do not change without re-running full backtest):
    - Donchian breakout uses SHIFTED-1 high/low (yesterday's boundary, not today's)
    - Stage 7 does NOT require volume surge (volume is informational only)
    - Stage 2 prerequisite is enforced at SIGNAL GENERATOR level, not here
    - Stage 2 requires ALL FOUR conditions simultaneously
    - Volume surge uses strict > (not >=)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd

# ── Stage registry ─────────────────────────────────────────────────────────────
STAGE_NAMES = {
    0: "Undefined",
    1: "Not Eligible",
    2: "Sharp Downtrend",
    3: "Downtrend",
    4: "Below Zone",
    5: "Lower Zone",
    6: "Breakout",
    7: "Breakout Confirmed",
    8: "In-Zone",
    9: "In-Zone (Fading)",
}


def _s(row: Any, col: str, default: Any = np.nan) -> Any:
    """Safe attribute getter — returns default on missing or NaN."""
    try:
        v = getattr(row, col)
        if pd.isna(v):
            return default
        return v
    except AttributeError:
        return default


def _ok(v: Any) -> bool:
    """True if value is a valid non-NaN number."""
    if v is None:
        return False
    try:
        return not np.isnan(float(v))
    except (TypeError, ValueError):
        return False


def classify_row(row: Any, stage2_seen: bool, cfg: Dict[str, Any]) -> int:
    """
    Classify one OHLCV+indicators row into a stage (1–9).

    Evaluation order: 2, 3, 4, 5, 7, 6, 9, 8, 1
    stage2_seen: expanding-window flag — True if Stage 2 ever printed for this ticker.
                 NOTE: stage2_seen does NOT gate Stage 7 here (backtest design).
                 The Stage 2 prerequisite is applied in signal_generator.py.
    """
    close = _s(row, "close", np.nan)
    if not _ok(close):
        return 1   # insufficient data

    # ── Indicators ────────────────────────────────────────────────────────────
    ema10   = _s(row, "ema_10",   np.nan)
    ema20   = _s(row, "ema_20",   np.nan)
    ema50   = _s(row, "ema_50",   np.nan)
    ema200  = _s(row, "ema_200",  np.nan)
    bb_lower = _s(row, "bb_lower", np.nan)
    bb_mid   = _s(row, "bb_mid",   np.nan)
    bb_upper = _s(row, "bb_upper", np.nan)
    # Shifted-1 Donchian: use yesterday's high/low (point-in-time safe breakout detection)
    don_high_s1 = _s(row, "donchian_upper_s1", np.nan)
    don_low_s1  = _s(row, "donchian_lower_s1", np.nan)
    vol_surge   = bool(_s(row, "volume_surge", False))

    # ── Derived flags (match backtest variable names exactly) ─────────────────
    below_ema200    = _ok(ema200) and close < ema200
    above_ema200    = _ok(ema200) and close > ema200
    bearish_stack   = (_ok(ema10) and _ok(ema20) and _ok(ema50) and _ok(ema200)
                       and ema10 < ema20 < ema50 < ema200)
    trend_turning_up = _ok(ema10) and _ok(ema20) and ema10 > ema20
    recovered       = _ok(ema50) and close > ema50
    breakout        = _ok(don_high_s1) and close > don_high_s1   # above yesterday's Donchian high
    breakdown       = _ok(don_low_s1)  and close < don_low_s1    # below yesterday's Donchian low

    # ── Stage 2 — Sharp Downtrend (dislocation prerequisite setter) ──────────
    # ALL FOUR required: below EMA200 + bearish EMA stack + Donchian low breakdown + vol surge
    if below_ema200 and bearish_stack and breakdown and vol_surge:
        return 2

    # ── Stage 3 — Downtrend ───────────────────────────────────────────────────
    # Below EMA200, in full bearish EMA stack, but no Donchian breakdown + vol surge
    if below_ema200 and bearish_stack:
        return 3

    # ── Stage 4 — Below Zone (stabilising at bottom) ─────────────────────────
    # Below EMA200, price at or below lower Bollinger Band (statistically oversold)
    if below_ema200 and _ok(bb_lower) and close <= bb_lower:
        return 4

    # ── Stage 5 — Lower Zone (base forming) ──────────────────────────────────
    # Below EMA200, price between BB lower and BB mid (recovering from oversold)
    if below_ema200 and _ok(bb_lower) and _ok(bb_mid) and bb_lower < close < bb_mid:
        return 5

    # ── Stage 7 — Breakout Confirmed (PRIMARY ENTRY — checked BEFORE Stage 6) ─
    # close > yesterday's Donchian high + close > EMA50 + EMA10 > EMA20
    # Volume is NOT required (backtest design — vol adds R-reasons but not gated)
    if breakout and recovered and trend_turning_up:
        return 7

    # ── Stage 6 — Breakout (secondary entry) ─────────────────────────────────
    # close > yesterday's Donchian high + EMA10 > EMA20 (but NOT yet above EMA50)
    if breakout and trend_turning_up:
        return 6

    # ── Stage 9 — In-Zone Fading (EXIT signal) ────────────────────────────────
    # Above EMA200 but close has fallen below EMA10 (momentum turning)
    if above_ema200 and _ok(ema10) and close < ema10:
        return 9

    # ── Stage 8 — In-Zone (hold, position active) ────────────────────────────
    # Simply above EMA200 — trend intact, position continuing
    if above_ema200:
        return 8

    # ── Stage 1 — Not Eligible (default) ─────────────────────────────────────
    return 1


def classify_stages(df: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    """
    Row-by-row stage classification with expanding Stage 2 memory.
    Returns df with appended columns: stage, stage_name, stage2_memory.
    """
    stages: list[dict] = []
    stage2_seen = False

    for row in df.itertuples(index=False):
        s = classify_row(row, stage2_seen, cfg)
        if s == 2:
            stage2_seen = True
        stages.append({
            "stage":        s,
            "stage_name":   STAGE_NAMES.get(s, "Unknown"),
            "stage2_memory": stage2_seen,
        })

    result = pd.DataFrame(stages, index=df.index)
    return pd.concat([df, result], axis=1)


class StageClassifier:
    """
    Stateful classifier for live production use.
    Persists Stage 2 memory across daily runs via state/stage2_memory.json.

    Stage 2 memory is EXPANDING + PERMANENT — once True, never resets.
    This matches backtest stage2_ever_seen logic (09A expanding cummax).
    """

    def __init__(self, ticker: str, state_dir: Path, cfg: Dict[str, Any]) -> None:
        self.ticker     = ticker
        self.state_dir  = Path(state_dir)
        self.cfg        = cfg
        self._mem_file  = self.state_dir / "stage2_memory.json"
        self._stage2_seen: bool = self._load()

    def _load(self) -> bool:
        if self._mem_file.exists():
            try:
                return bool(json.loads(self._mem_file.read_text(encoding="utf-8")).get(self.ticker, False))
            except Exception:
                return False
        return False

    def _persist(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if self._mem_file.exists():
            try:
                data = json.loads(self._mem_file.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[self.ticker] = self._stage2_seen
        self._mem_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def classify(self, df: pd.DataFrame) -> pd.DataFrame:
        """Classify full df; updates and persists Stage 2 memory."""
        stages: list[dict] = []
        for row in df.itertuples(index=False):
            s = classify_row(row, self._stage2_seen, self.cfg)
            if s == 2:
                self._stage2_seen = True
            stages.append({
                "stage":         s,
                "stage_name":    STAGE_NAMES.get(s, "Unknown"),
                "stage2_memory": self._stage2_seen,
            })
        self._persist()
        result = pd.DataFrame(stages, index=df.index)
        return pd.concat([df, result], axis=1)

    @property
    def stage2_memory(self) -> bool:
        return self._stage2_seen
