# core/features/technicals/pipeline.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import pandas as pd
import numpy as np


@dataclass
class IndicatorConfig:
    ema_periods: List[int] = field(default_factory=lambda: [10, 20, 50, 100, 200])
    bb_period: int = 20
    bb_std: float = 2.0
    donchian_period: int = 20
    atr_period: int = 14
    volume_avg_window: int = 20
    volume_surge_mult: float = 1.15
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_period: int = 14

    @classmethod
    def from_dict(cls, cfg: dict) -> "IndicatorConfig":
        return cls(
            ema_periods=cfg.get("ema", {}).get("periods", [10, 20, 50, 100, 200]),
            bb_period=cfg.get("bollinger_bands", {}).get("period", 20),
            bb_std=cfg.get("bollinger_bands", {}).get("std_dev", 2.0),
            donchian_period=cfg.get("donchian", {}).get("period", 20),
            atr_period=cfg.get("atr", {}).get("period", 14),
            volume_avg_window=cfg.get("volume", {}).get("avg_window", 20),
            volume_surge_mult=cfg.get("volume", {}).get("surge_multiplier", 1.15),
            macd_fast=cfg.get("macd", {}).get("fast", 12),
            macd_slow=cfg.get("macd", {}).get("slow", 26),
            macd_signal=cfg.get("macd", {}).get("signal", 9),
            rsi_period=cfg.get("rsi", {}).get("period", 14),
        )


def apply_indicators(df: pd.DataFrame, cfg: IndicatorConfig) -> pd.DataFrame:
    """
    Apply all technical indicators to OHLCV DataFrame.
    Input columns: open, high, low, close, volume (case-insensitive).
    Returns df with all indicator columns appended.
    """
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # ── EMAs ──────────────────────────────────────────────────────────────────
    # min_periods=p is REQUIRED for backtest parity: ALGO-Stocks'
    # features/technicals/indicators.py::ema() gates every EMA (and every
    # other rolling/ewm indicator below) with min_periods=<full window>, so
    # a ticker with fewer than `p` real bars gets NaN, not a numerically
    # "valid-looking" but statistically meaningless EMA computed over a
    # too-short window. Without this, short-history tickers (recent IPOs --
    # e.g. anything with <200 bars) would get a fake, non-NaN ema_200 fed
    # into stage classification instead of correctly falling through as
    # not-yet-eligible. Found and fixed 2026-08-17 while investigating why
    # production's cache had a batch of very-short-history tickers.
    for p in cfg.ema_periods:
        df[f"ema_{p}"] = close.ewm(span=p, adjust=False, min_periods=p).mean()

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    bb_mid = close.rolling(cfg.bb_period, min_periods=cfg.bb_period).mean()
    bb_std = close.rolling(cfg.bb_period, min_periods=cfg.bb_period).std(ddof=0)
    df["bb_mid"] = bb_mid
    df["bb_upper"] = bb_mid + cfg.bb_std * bb_std
    df["bb_lower"] = bb_mid - cfg.bb_std * bb_std
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / bb_mid.replace(0, np.nan)
    df["bb_pct"] = (close - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)

    # ── Donchian Channel ──────────────────────────────────────────────────────
    df["donchian_upper"] = high.rolling(cfg.donchian_period, min_periods=cfg.donchian_period).max()
    df["donchian_lower"] = low.rolling(cfg.donchian_period, min_periods=cfg.donchian_period).min()
    df["donchian_mid"]   = (df["donchian_upper"] + df["donchian_lower"]) / 2
    # Shifted by 1 bar — backtest uses yesterday's Donchian high/low for breakout detection
    # (point-in-time safe: today's close vs yesterday's range boundary)
    df["donchian_upper_s1"] = df["donchian_upper"].shift(1)
    df["donchian_lower_s1"] = df["donchian_lower"].shift(1)

    # ── ATR ───────────────────────────────────────────────────────────────────
    # (already min_periods-gated correctly elsewhere -- signal_generator.py's
    # _compute_atr uses min_periods=period; this copy here is used by the
    # Stage classifier pipeline, matching the same gating for consistency.)
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=cfg.atr_period, adjust=False, min_periods=cfg.atr_period).mean()

    # ── Volume ────────────────────────────────────────────────────────────────
    df["volume_avg"] = volume.rolling(cfg.volume_avg_window, min_periods=cfg.volume_avg_window).mean()
    df["volume_ratio"] = volume / df["volume_avg"].replace(0, np.nan)
    df["volume_surge"] = df["volume_ratio"] > cfg.volume_surge_mult   # backtest uses strict >

    # ── Rate of Change (5-day) — used for Stage 2 rapid decline detection ────
    # Backtest requires >5% decline over 3-5 trading days for genuine dislocation
    df["roc_5d"] = close.pct_change(5)

    # ── MACD ──────────────────────────────────────────────────────────────────
    ema_fast = close.ewm(span=cfg.macd_fast, adjust=False, min_periods=cfg.macd_fast).mean()
    ema_slow = close.ewm(span=cfg.macd_slow, adjust=False, min_periods=cfg.macd_slow).mean()
    df["macd"] = ema_fast - ema_slow
    df["macd_signal"] = df["macd"].ewm(span=cfg.macd_signal, adjust=False, min_periods=cfg.macd_signal).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # ── RSI ───────────────────────────────────────────────────────────────────
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / cfg.rsi_period, adjust=False, min_periods=cfg.rsi_period).mean()
    avg_loss = loss.ewm(alpha=1 / cfg.rsi_period, adjust=False, min_periods=cfg.rsi_period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    return df
